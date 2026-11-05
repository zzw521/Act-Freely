//! 应用更新清单（缺陷 D-12，#159）：格式定义、严格校验与逐产物验签。
//!
//! 这一份只管「清单说了什么、能不能信」，不碰网络也不碰磁盘——
//! 取回、暂存、安装、回滚在 `app_update/mod.rs`。分开的原因是这两半的失败方式
//! 完全不同：清单错了是**别人的数据不可信**，必须当场拒掉；文件操作错了是本机
//! 盘满或权限不足，要能重试。混在一起写就会出现「下载失败了所以顺手把清单也当没验过」。
//!
//! ## 清单格式（`tt-app-update/1`）
//!
//! ```json
//! {
//!   "schema": "tt-app-update/1",
//!   "channel": "stable",
//!   "publishedAtMs": 1730000000000,
//!   "revokedVersions": ["0.1.5"],
//!   "releases": [
//!     {
//!       "version": "0.2.0",
//!       "minAppVersion": "0.1.0",
//!       "notes": "本次改动说明",
//!       "artifacts": [
//!         {
//!           "platform": "windows", "arch": "x64", "kind": "nsis",
//!           "url": "https://example.invalid/renwoxing-x64-0.2.0.exe",
//!           "sha256": "<64 位十六进制>", "sizeBytes": 81234567,
//!           "publisherId": "tongtian-official", "signature": "<64 字节 Ed25519 的 base64>"
//!         }
//!       ]
//!     }
//!   ]
//! }
//! ```
//!
//! ## 三条不妥协的口径
//!
//! - **没有「可不签名」这一档**。市场索引有 `signaturePolicy=optional`，更新清单没有：
//!   市场装的是随时能禁用的技能，更新换的是整个应用。字段表里干脆不接受这个键
//!   （`deny_unknown_fields`），写了宽松档的清单会被当格式错误整条拒掉，
//!   而不是被读成一个我们其实没实现的开关。
//! - **签名域是单独一条 `tt-app-update-v1`**，与市场内容签名用的 `tt-market-v1` 不同名。
//!   同一组字段在两域下签名载荷不同，市场发布者的签名永远当不了更新清单的签名。
//! - **未知字段一律拒**。清单是别人写的、会演进的；今天实现读不到的键如果默默放过，
//!   将来加一个 `requireApproval` 之类的键就会被旧版本读成「没有这个要求」。

use base64::Engine as _;
use ring::signature::{UnparsedPublicKey, ED25519};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

/// 清单格式版本；改格式就改这里，并同步 `sample_manifest_json` 与契约测试。
pub const SCHEMA: &str = "tt-app-update/1";

/// 签名域：与 `market::SIGNING_DOMAIN`（`tt-market-v1`）故意不同名，两份签名不可互用。
pub const SIGNING_DOMAIN: &str = "tt-app-update-v1";

pub const MAX_RELEASES: usize = 200;
pub const MAX_ARTIFACTS_PER_RELEASE: usize = 12;
pub const MAX_REVOKED_VERSIONS: usize = 200;
pub const MAX_CHANNEL_CHARS: usize = 32;
pub const MAX_NOTES_CHARS: usize = 4000;
pub const MAX_URL_LEN: usize = 2048;
pub const MAX_ID_CHARS: usize = 64;
/// 安装包不可能小于 1 KiB；小于这个数只会是清单写错了或产物被截断。
pub const MIN_ARTIFACT_BYTES: u64 = 1024;
/// 单个安装包的硬上限（2 GiB）。声明值超过它直接拒，不给「下满磁盘」留机会。
pub const MAX_ARTIFACT_BYTES: u64 = 2 * 1024 * 1024 * 1024;

pub const KIND_NSIS: &str = "nsis";
pub const KIND_MSI: &str = "msi";

pub const PLATFORM_WINDOWS: &str = "windows";
pub const PLATFORM_MACOS: &str = "macos";
pub const PLATFORM_LINUX: &str = "linux";

pub const ARCH_X64: &str = "x64";
pub const ARCH_ARM64: &str = "arm64";

/// 本模块支持的产物类型（裸 `exe` 不在列：没有安装器就没有「带回滚的替换」可言）。
pub fn kind_extension(kind: &str) -> Option<&'static str> {
    match kind {
        KIND_NSIS => Some("exe"),
        KIND_MSI => Some("msi"),
        _ => None,
    }
}

pub const KINDS: &[&str] = &[KIND_NSIS, KIND_MSI];
pub const PLATFORMS: &[&str] = &[PLATFORM_WINDOWS, PLATFORM_MACOS, PLATFORM_LINUX];
pub const ARCHES: &[&str] = &[ARCH_X64, ARCH_ARM64];

/// 本机在哪一格：清单里对不上这一格的产物不参与判定。
pub fn host_platform() -> &'static str {
    if cfg!(target_os = "windows") {
        PLATFORM_WINDOWS
    } else if cfg!(target_os = "macos") {
        PLATFORM_MACOS
    } else {
        PLATFORM_LINUX
    }
}

pub fn host_arch() -> &'static str {
    match std::env::consts::ARCH {
        "x86_64" => ARCH_X64,
        "aarch64" => ARCH_ARM64,
        other => other,
    }
}

// ── 小工具（口径与市场一致，但各写一份：market 是别的窗口的领地，不能伸手改）──

pub fn sha256_hex(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    hasher.finalize().iter().map(|b| format!("{b:02x}")).collect()
}

pub fn decode_base64(raw: &str) -> Option<Vec<u8>> {
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

/// 公钥指纹 = 公钥原始字节的 SHA-256。展示口径与市场发布者一致（前 16 位 + 省略号），
/// 用户在两处核对同一把钥匙时看到的是同一串。
pub fn fingerprint_of_key_b64(public_key: &str) -> String {
    decode_base64(public_key)
        .map(|bytes| sha256_hex(&bytes))
        .unwrap_or_default()
}

pub fn short_fingerprint(fingerprint: &str) -> String {
    format!("{}…", &fingerprint[..fingerprint.len().min(16)])
}

fn bad_manifest(detail: impl Into<String>) -> String {
    format!("UPDATE_INVALID_MANIFEST: {}", detail.into())
}

// ── 版本号 ──────────────────────────────────────────────────────

/// 严格版本号：`主.次.补丁[-预发布]`。十进制段 1—9 位、不许前导零；
/// 段值超过 9 位就无法安全转 u64，宁可拒掉也不留「看着能比」的版本。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Version {
    pub raw: String,
    major: u64,
    minor: u64,
    patch: u64,
    pre: Option<String>,
}

fn zeroed(raw: &str) -> Version {
    Version {
        raw: raw.to_string(),
        major: 0,
        minor: 0,
        patch: 0,
        pre: None,
    }
}

fn parse_numeric(part: &str) -> Result<u64, String> {
    if part.is_empty() || part.len() > 9 || !part.bytes().all(|b| b.is_ascii_digit()) {
        return Err(format!("版本段「{part}」不是 1—9 位十进制数字"));
    }
    if part.len() > 1 && part.starts_with('0') {
        return Err(format!("版本段「{part}」不允许前导零"));
    }
    Ok(part.parse::<u64>().unwrap_or(u64::MAX))
}

/// 预发布标签：只许字母、数字、点与连字符，按点分段逐段字典序比较。
/// 这条规则如实写进 `update_describe` 的 `versionRule`，不冒充完整 semver 2.0
/// （完整 semver 还要比「数字段小于数字段」，本机不需要那个精度，也不需要那个坑）。
fn parse_pre(raw: &str) -> Result<Option<String>, String> {
    let body = raw.trim_start_matches('-');
    if body.is_empty() {
        return Ok(None);
    }
    let pre = body.to_ascii_lowercase();
    if pre.len() > 40
        || !pre
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'-'))
    {
        return Err(format!(
            "预发布标签「{raw}」非法（只许字母、数字、点与连字符，≤40 字符）"
        ));
    }
    Ok(Some(pre))
}

impl Version {
    pub fn parse(raw: &str) -> Result<Version, String> {
        let trimmed = raw.trim();
        if trimmed.is_empty() || trimmed.len() > 64 {
            return Err("版本号不能为空且长度不超过 64 字符".to_string());
        }
        // 允许 "v1.2.3" 的写法进得来，但落账一律用去掉 v 的规范串
        let body = match trimmed.as_bytes().first() {
            Some(b'v') | Some(b'V') => &trimmed[1..],
            _ => trimmed,
        };
        let (core, pre_raw) = match body.split_once('-') {
            Some((a, b)) => (a, Some(b)),
            None => (body, None),
        };
        let mut it = core.split('.');
        let major = parse_numeric(it.next().unwrap_or_default())?;
        let minor = parse_numeric(it.next().unwrap_or_default())?;
        let patch = parse_numeric(it.next().unwrap_or_default())?;
        if it.next().is_some() {
            return Err(format!("版本号「{raw}」必须是 主.次.补丁 三段"));
        }
        let pre = match pre_raw {
            Some(p) => parse_pre(p)?,
            None => None,
        };
        Ok(Version {
            raw: body.to_ascii_lowercase(),
            major,
            minor,
            patch,
            pre,
        })
    }

    /// 手写比较，不用 derive：预发布版本必须**低于**同号正式版，
    /// 而 derive 出来的字典序会把 `1.0.0-beta` 排在 `1.0.0` 之后——
    /// 那等于允许清单拿一个旧的预发布包冒充新版。
    pub fn cmp_version(&self, other: &Version) -> std::cmp::Ordering {
        use std::cmp::Ordering;
        match (self.major, self.minor, self.patch).cmp(&(other.major, other.minor, other.patch)) {
            Ordering::Equal => {}
            other => return other,
        }
        match (&self.pre, &other.pre) {
            (None, None) => Ordering::Equal,
            (None, Some(_)) => Ordering::Greater,
            (Some(_), None) => Ordering::Less,
            (Some(a), Some(b)) => {
                let a: Vec<&str> = a.split('.').collect();
                let b: Vec<&str> = b.split('.').collect();
                for (x, y) in a.iter().zip(b.iter()) {
                    match x.cmp(y) {
                        Ordering::Equal => {}
                        o => return o,
                    }
                }
                a.len().cmp(&b.len())
            }
        }
    }

    pub fn is_newer_than(&self, current: &Version) -> bool {
        self.cmp_version(current) == std::cmp::Ordering::Greater
    }
}

// ── 线上格式（只用来反序列化；字段一个都不直接对外）──

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct RawArtifact {
    platform: String,
    arch: String,
    kind: String,
    url: String,
    sha256: String,
    size_bytes: u64,
    publisher_id: String,
    signature: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct RawRelease {
    version: String,
    #[serde(default)]
    min_app_version: Option<String>,
    #[serde(default)]
    notes: String,
    artifacts: Vec<RawArtifact>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct RawManifest {
    schema: String,
    channel: String,
    published_at_ms: i64,
    #[serde(default)]
    revoked_versions: Vec<String>,
    releases: Vec<RawRelease>,
}

// ── 校验后的模型 ────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Artifact {
    pub platform: String,
    pub arch: String,
    pub kind: String,
    pub url: String,
    /// 已归一为小写十六进制的 64 位哈希；下游再比对时不必规范化第二次
    pub sha256: String,
    pub size_bytes: u64,
    pub publisher_id: String,
    #[serde(skip)]
    pub signature: Vec<u8>,
    /// 签名的规范化 base64 展示串（与清单里那份等价）
    pub signature_b64: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Release {
    pub version: String,
    pub min_app_version: Option<String>,
    pub notes: String,
    pub artifacts: Vec<Artifact>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Manifest {
    pub channel: String,
    pub published_at_ms: i64,
    pub revoked_versions: Vec<String>,
    pub releases: Vec<Release>,
}

fn require_id(raw: &str, what: &str) -> Result<String, String> {
    let id = raw.trim().to_ascii_lowercase();
    if id.is_empty() || id.len() > MAX_ID_CHARS {
        return Err(format!("{what}长度必须是 1—{MAX_ID_CHARS} 字符"));
    }
    if !id
        .bytes()
        .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'.' | b'_' | b'-'))
    {
        return Err(format!("{what}只能包含字母、数字、点、下划线与连字符"));
    }
    if id.starts_with('.') || id.starts_with('-') {
        return Err(format!("{what}不能以点或连字符开头"));
    }
    Ok(id)
}

/// 渠道名的唯一校验处：清单里的 `channel` 与本机存的渠道名走同一个函数、同一套规则，
/// 否则「本机配了 Stable、清单写 stable」这种大小写差异会让取清单永远拿不到。
pub fn require_channel(raw: &str) -> Result<String, String> {
    let ch = raw.trim().to_ascii_lowercase();
    if ch.len() > MAX_CHANNEL_CHARS {
        return Err(format!("渠道名长度不能超过 {MAX_CHANNEL_CHARS} 字符"));
    }
    require_id(&ch, "渠道名")
}

fn where_artifact(index: usize, artifact_index: usize) -> String {
    format!("第 {} 个发布的第 {} 个产物", index + 1, artifact_index + 1)
}

fn validate_url(raw: &str, index: usize, artifact_index: usize) -> Result<String, String> {
    let at = where_artifact(index, artifact_index);
    if raw.len() > MAX_URL_LEN {
        return Err(format!("{at}地址过长（上限 {MAX_URL_LEN} 字符）"));
    }
    let mut url =
        reqwest::Url::parse(raw).map_err(|_| format!("{at}地址不是完整 URL，请填写 http(s) 全地址"))?;
    if !url.username().is_empty() || url.password().is_some() {
        return Err(format!("{at}地址里不能带用户名或密码"));
    }
    url.set_fragment(None);
    if url.host_str().is_none() {
        return Err(format!("{at}地址缺少主机名"));
    }
    if !matches!(url.scheme(), "https" | "http") {
        return Err(format!("{at}地址只支持 http / https"));
    }
    Ok(url.to_string())
}

fn validate_sha(raw: &str, index: usize, artifact_index: usize) -> Result<String, String> {
    let value = raw.trim().to_ascii_lowercase();
    if value.len() != 64 || !value.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(format!(
            "{}的 sha256 必须是 64 位十六进制",
            where_artifact(index, artifact_index)
        ));
    }
    Ok(value)
}

fn validate_signature(raw: &str, index: usize, artifact_index: usize) -> Result<(Vec<u8>, String), String> {
    let at = where_artifact(index, artifact_index);
    let bytes = decode_base64(raw).ok_or_else(|| format!("{at}的 signature 不是合法 base64"))?;
    if bytes.len() != 64 {
        return Err(format!(
            "{at}的 signature 必须是 64 字节 Ed25519 签名（实际 {} 字节）",
            bytes.len()
        ));
    }
    Ok((
        bytes.clone(),
        base64::engine::general_purpose::STANDARD_NO_PAD.encode(&bytes),
    ))
}

/// 清单文案进界面与日志之前先压平控制字符（换行保留）：
/// 留 `\u{0}` 这类字符等于允许清单伪造分隔符。
fn sanitize_notes(raw: &str) -> String {
    raw.chars()
        .map(|c| {
            if c == '\n' || c == '\r' {
                '\n'
            } else if c.is_control() {
                ' '
            } else {
                c
            }
        })
        .collect::<String>()
        .trim()
        .to_string()
}

/// 解析并校验清单；`want_channel` 是本机渠道配置指定的渠道，两者不一致直接拒。
///
/// 拒的理由都带位置（第几个发布、第几个产物）——清单是人手写的，
/// 没有位置的报错等于让运维再猜一轮。
pub fn parse_manifest(bytes: &[u8], want_channel: &str) -> Result<Manifest, String> {
    let raw: RawManifest = serde_json::from_slice(bytes)
        .map_err(|e| bad_manifest(format!("清单不是合法的 {SCHEMA} JSON（{e}）")))?;
    if raw.schema != SCHEMA {
        return Err(bad_manifest(format!(
            "清单版本是「{}」，本机只认「{SCHEMA}」",
            raw.schema
        )));
    }
    let channel = require_channel(&raw.channel).map_err(bad_manifest)?;
    let want = require_channel(want_channel).map_err(bad_manifest)?;
    if channel != want {
        return Err(bad_manifest(format!(
            "清单渠道是「{channel}」，本机渠道配置要的是「{want}」，已拒绝（防止渠道串台）"
        )));
    }
    if raw.published_at_ms <= 0 {
        return Err(bad_manifest("publishedAtMs 必须是正整数毫秒"));
    }
    if raw.releases.is_empty() {
        return Err(bad_manifest("清单里没有任何发布（releases 为空）"));
    }
    if raw.releases.len() > MAX_RELEASES {
        return Err(bad_manifest(format!("清单发布数超过 {MAX_RELEASES} 条上限")));
    }
    if raw.revoked_versions.len() > MAX_REVOKED_VERSIONS {
        return Err(bad_manifest(format!(
            "撤销清单超过 {MAX_REVOKED_VERSIONS} 条上限"
        )));
    }
    let revoked = raw
        .revoked_versions
        .iter()
        .map(|v| {
            Version::parse(v)
                .map(|p| p.raw)
                .map_err(|e| bad_manifest(format!("撤销清单里的版本号非法（{e}）")))
        })
        .collect::<Result<Vec<_>, _>>()?;

    let mut releases: Vec<Release> = Vec::with_capacity(raw.releases.len());
    let mut seen: Vec<String> = Vec::with_capacity(raw.releases.len());
    for (index, r) in raw.releases.into_iter().enumerate() {
        let version = Version::parse(&r.version).map_err(|e| {
            bad_manifest(format!("第 {} 个发布的版本号非法（{e}）", index + 1))
        })?;
        if seen.contains(&version.raw) {
            return Err(bad_manifest(format!(
                "版本号「{}」在清单里出现了两次，已拒绝",
                version.raw
            )));
        }
        seen.push(version.raw.clone());
        let min_app_version = match r.min_app_version.as_deref() {
            None => None,
            Some(v) => Some(Version::parse(v).map(|p| p.raw).map_err(|e| {
                bad_manifest(format!("第 {} 个发布的 minAppVersion 非法（{e}）", index + 1))
            })?),
        };
        let notes = sanitize_notes(&r.notes);
        if notes.chars().count() > MAX_NOTES_CHARS {
            return Err(bad_manifest(format!(
                "第 {} 个发布的 notes 超过 {MAX_NOTES_CHARS} 字上限",
                index + 1
            )));
        }
        if r.artifacts.is_empty() {
            return Err(bad_manifest(format!("第 {} 个发布没有任何产物", index + 1)));
        }
        if r.artifacts.len() > MAX_ARTIFACTS_PER_RELEASE {
            return Err(bad_manifest(format!(
                "第 {} 个发布的产物数超过 {MAX_ARTIFACTS_PER_RELEASE} 个上限",
                index + 1
            )));
        }
        let mut artifacts: Vec<Artifact> = Vec::with_capacity(r.artifacts.len());
        for (artifact_index, a) in r.artifacts.into_iter().enumerate() {
            let at = where_artifact(index, artifact_index);
            let platform = a.platform.trim().to_ascii_lowercase();
            if !PLATFORMS.contains(&platform.as_str()) {
                return Err(bad_manifest(format!(
                    "{at}平台「{}」不在支持表（{}）",
                    platform,
                    PLATFORMS.join(" / ")
                )));
            }
            let arch = a.arch.trim().to_ascii_lowercase();
            if !ARCHES.contains(&arch.as_str()) {
                return Err(bad_manifest(format!(
                    "{at}架构「{}」不在支持表（{}）",
                    arch,
                    ARCHES.join(" / ")
                )));
            }
            let kind = a.kind.trim().to_ascii_lowercase();
            if kind_extension(&kind).is_none() {
                return Err(bad_manifest(format!(
                    "{at}类型「{kind}」不支持（只认 {}）",
                    KINDS.join(" / ")
                )));
            }
            if artifacts
                .iter()
                .any(|x| x.platform == platform && x.arch == arch)
            {
                return Err(bad_manifest(format!(
                    "第 {} 个发布里 {platform}-{arch} 有两个产物，取哪一个不该靠顺序决定",
                    index + 1
                )));
            }
            let url = validate_url(&a.url, index, artifact_index).map_err(bad_manifest)?;
            let sha256 = validate_sha(&a.sha256, index, artifact_index).map_err(bad_manifest)?;
            if a.size_bytes < MIN_ARTIFACT_BYTES || a.size_bytes > MAX_ARTIFACT_BYTES {
                return Err(bad_manifest(format!(
                    "{at}声明大小 {} 字节不在 {}—{} 字节之间",
                    a.size_bytes,
                    MIN_ARTIFACT_BYTES,
                    MAX_ARTIFACT_BYTES
                )));
            }
            let publisher_id = require_id(&a.publisher_id, "发布者标识")
                .map_err(|e| bad_manifest(format!("{at}（{e}）")))?;
            let (signature, signature_b64) =
                validate_signature(&a.signature, index, artifact_index).map_err(bad_manifest)?;
            artifacts.push(Artifact {
                platform,
                arch,
                kind,
                url,
                sha256,
                size_bytes: a.size_bytes,
                publisher_id,
                signature,
                signature_b64,
            });
        }
        releases.push(Release {
            version: version.raw,
            min_app_version,
            notes,
            artifacts,
        });
    }

    Ok(Manifest {
        channel,
        published_at_ms: raw.published_at_ms,
        revoked_versions: revoked,
        releases,
    })
}

// ── 验签 ────────────────────────────────────────────────────────

/// 一把被本机信任的发布钥。`source` 只有两个值：
/// `embedded`（随包编译进来的锚点，本机删不掉）与 `local`（用户在本机加的，可删）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Anchor {
    pub publisher_id: String,
    /// 规范化后的 base64（32 字节 Ed25519 公钥，无填充）
    pub public_key: String,
    pub fingerprint: String,
    pub source: String,
}

impl Anchor {
    /// 由用户输入的公钥串造锚点；不是 32 字节的钥匙一律拒（不接受「看着像」的公钥）。
    pub fn from_raw(publisher_id: &str, raw_key: &str, source: &str) -> Result<Anchor, String> {
        let id = require_id(publisher_id, "发布者标识")?;
        let bytes = decode_base64(raw_key)
            .ok_or_else(|| "UPDATE_INVALID_PUBLISHER_KEY: 公钥不是合法的 base64".to_string())?;
        if bytes.len() != 32 {
            return Err(format!(
                "UPDATE_INVALID_PUBLISHER_KEY: 公钥必须是 32 字节 Ed25519 公钥（实际 {} 字节）",
                bytes.len()
            ));
        }
        let public_key = base64::engine::general_purpose::STANDARD_NO_PAD.encode(&bytes);
        Ok(Anchor {
            publisher_id: id,
            public_key: public_key.clone(),
            fingerprint: fingerprint_of_key_b64(&public_key),
            source: source.to_string(),
        })
    }
}

/// 签名的覆盖范围：域 + 渠道 + 版本 + 平台 + 架构 + 产物类型 + 发布者 + 产物哈希。
///
/// `notes` 这类展示文案**不在**签名里：改了文案不该让已签好的安装包失效。
/// 渠道**在**签名里：同一条签名不能从 `stable` 搬到 `beta` 用。
pub fn signing_payload(
    channel: &str,
    version: &str,
    platform: &str,
    arch: &str,
    kind: &str,
    publisher_id: &str,
    sha256: &str,
) -> Vec<u8> {
    format!(
        "{SIGNING_DOMAIN}\n{channel}\n{version}\n{platform}\n{arch}\n{kind}\n{publisher_id}\n{sha256}"
    )
    .into_bytes()
}

/// 逐个产物验签，返回命中的锚点（界面要显示「是哪把钥匙签的」）。
///
/// 三种拒法分开写，因为运维要做的动作不一样：
/// - 本机没有任何锚点 ⇒ 先去添加公钥（今天内嵌锚点为空，这是常态不是异常）；
/// - 有这个发布者但本机没信任 ⇒ 核对指纹后再加；
/// - 验不过 ⇒ 明确说「签名与内容不匹配」，不猜是哪一个字段变了。
pub fn verify_artifact(
    manifest: &Manifest,
    release: &Release,
    artifact: &Artifact,
    anchors: &[Anchor],
) -> Result<Anchor, String> {
    if anchors.is_empty() {
        return Err(
            "UPDATE_NO_TRUST_ANCHOR: 本机还没有任何受信任的发布公钥，更新清单无法验签，已拒绝"
                .to_string(),
        );
    }
    let mine: Vec<&Anchor> = anchors
        .iter()
        .filter(|a| a.publisher_id == artifact.publisher_id)
        .collect();
    if mine.is_empty() {
        return Err(format!(
            "UPDATE_UNTRUSTED_PUBLISHER: 清单声明的发布者「{}」不在本机信任列表，已拒绝（先核对指纹再添加公钥）",
            artifact.publisher_id
        ));
    }
    let payload = signing_payload(
        &manifest.channel,
        &release.version,
        &artifact.platform,
        &artifact.arch,
        &artifact.kind,
        &artifact.publisher_id,
        &artifact.sha256,
    );
    for anchor in &mine {
        let key = decode_base64(&anchor.public_key).unwrap_or_default();
        if UnparsedPublicKey::new(&ED25519, &key)
            .verify(&payload, &artifact.signature)
            .is_ok()
        {
            return Ok((*anchor).clone());
        }
    }
    let trusted = mine
        .iter()
        .map(|a| short_fingerprint(&a.fingerprint))
        .collect::<Vec<_>>()
        .join(" / ");
    Err(format!(
        "UPDATE_SIGNATURE_INVALID: 发布者「{}」的签名与版本「{}」的 {}-{} 产物不匹配（本机已信任指纹 {trusted}），已拒绝",
        artifact.publisher_id, release.version, artifact.platform, artifact.arch
    ))
}

// ── 判定：这台机器该装哪一条 ────────────────────────────────────

pub fn artifact_view(art: &Artifact) -> Value {
    json!({
        "platform": art.platform,
        "arch": art.arch,
        "kind": art.kind,
        "url": art.url,
        "sha256": art.sha256,
        "sizeBytes": art.size_bytes,
        "publisherId": art.publisher_id,
        "fileExtension": kind_extension(&art.kind).unwrap_or("bin"),
    })
}

/// 对清单做一次「本机能不能用」的形状判定，逐条给出理由；不验签、不改状态、不碰盘。
///
/// 降级一律拒绝：比当前版本旧的条目 `newer=false`。回滚走的是本机留存的安装包
/// （`update_rollback`），不经过清单——否则一个被攻破的清单就能把本机「更新」到带漏洞的旧版。
pub fn evaluate(manifest: &Manifest, current_version: &str) -> Value {
    let current = match Version::parse(current_version) {
        Ok(v) => v,
        // 当前版本读不出来时不猜：整份清单判为不可用，并把原因如实回出来
        Err(e) => {
            return json!({
                "rows": [],
                "installable": null,
                "notices": [format!("本机版本「{current_version}」读不出可比格式（{e}）")],
                "host": {"platform": host_platform(), "arch": host_arch()},
                "currentVersion": current_version,
            })
        }
    };
    let platform = host_platform();
    let arch = host_arch();
    let mut rows: Vec<Value> = Vec::new();
    let mut notices: Vec<String> = Vec::new();
    let mut best: Option<(usize, usize, Version)> = None;

    for release in &manifest.releases {
        let parsed = Version::parse(&release.version).unwrap_or_else(|_| zeroed(&release.version));
        let newer = parsed.is_newer_than(&current);
        let revoked = manifest.revoked_versions.contains(&release.version);
        let min_ok = match release.min_app_version.as_deref() {
            None => true,
            Some(min) => Version::parse(min)
                .map(|m| m.cmp_version(&current) != std::cmp::Ordering::Greater)
                .unwrap_or(false),
        };
        let position = release
            .artifacts
            .iter()
            .position(|a| a.platform == platform && a.arch == arch);
        let mut reasons: Vec<String> = Vec::new();
        if !newer {
            reasons.push("不比本机版本新（清单不许降级）".to_string());
        }
        if revoked {
            reasons.push("这个版本已被清单撤销".to_string());
        }
        if !min_ok {
            reasons.push(format!(
                "要求本机版本不低于 {}",
                release.min_app_version.clone().unwrap_or_default()
            ));
        }
        let art = position.map(|i| &release.artifacts[i]);
        if art.is_none() {
            reasons.push(format!("本机是 {platform}-{arch}，清单没有对应产物"));
        }
        let installable = newer && !revoked && min_ok && art.is_some();
        rows.push(json!({
            "version": release.version,
            "notes": release.notes,
            "newer": newer,
            "revoked": revoked,
            "minAppVersion": release.min_app_version,
            "minAppVersionOk": min_ok,
            "artifact": art.map(artifact_view),
            "installable": installable,
            "reasons": reasons,
        }));
        if installable {
            let take = match &best {
                None => true,
                Some((_, _, previous)) => parsed.is_newer_than(previous),
            };
            if take {
                best = Some((rows.len() - 1, position.unwrap_or_default(), parsed));
            }
        }
    }

    let installable = best.map(|(release_index, artifact_index, _)| {
        json!({
            "version": manifest.releases[release_index].version,
            "artifact": artifact_view(&manifest.releases[release_index].artifacts[artifact_index]),
        })
    });
    let revoked_visible = manifest
        .releases
        .iter()
        .filter(|r| manifest.revoked_versions.contains(&r.version))
        .count();
    if revoked_visible > 0 {
        notices.push(format!("清单撤销了 {revoked_visible} 个本机可见的版本"));
    }
    if manifest.revoked_versions.contains(&current.raw) {
        notices.push(format!(
            "本机当前版本 {} 已被清单撤销，建议尽快更新",
            current.raw
        ));
    }
    json!({
        "rows": rows,
        "installable": installable,
        "notices": notices,
        "host": {"platform": platform, "arch": arch},
        "currentVersion": current.raw,
    })
}

/// 样例清单：`update_describe` 直接回给界面与运维当模板。
///
/// 这份样例**必须能被 `parse_manifest` 读进来**（有自测守住），否则文档格式和代码格式
/// 会在下一次改动时悄悄分叉——那正是 D-12 之前「清单格式没人说清」的老问题。
pub fn sample_manifest_json() -> String {
    json!({
        "schema": SCHEMA,
        "channel": "stable",
        "publishedAtMs": 1_730_000_000_000_i64,
        "revokedVersions": ["0.1.5"],
        "releases": [{
            "version": "0.2.0",
            "minAppVersion": "0.1.0",
            "notes": "样例：这一条的 sha256 与签名都是占位值，真实清单必须由发布方重新签",
            "artifacts": [{
                "platform": PLATFORM_WINDOWS,
                "arch": ARCH_X64,
                "kind": KIND_NSIS,
                "url": "https://update.example.invalid/renwoxing-x64-0.2.0.exe",
                "sha256": "0".repeat(64),
                "sizeBytes": 81_234_567_u64,
                "publisherId": "tongtian-official",
                "signature": "A".repeat(86),
            }],
        }],
    })
    .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use ring::signature::KeyPair as _;

    fn keypair() -> (ring::signature::Ed25519KeyPair, String) {
        let rng = ring::rand::SystemRandom::new();
        let pkcs8 = ring::signature::Ed25519KeyPair::generate_pkcs8(&rng).unwrap();
        let pair = ring::signature::Ed25519KeyPair::from_pkcs8(pkcs8.as_ref()).unwrap();
        let public = base64::engine::general_purpose::STANDARD_NO_PAD.encode(pair.public_key().as_ref());
        (pair, public)
    }

    fn artifact_value() -> Value {
        json!({
            "platform": PLATFORM_WINDOWS,
            "arch": ARCH_X64,
            "kind": KIND_NSIS,
            "url": "https://update.example.invalid/a.exe",
            "sha256": "a".repeat(64),
            "sizeBytes": 1024_u64,
            "publisherId": "tongtian-official",
            "signature": "A".repeat(86),
        })
    }

    fn manifest_json(artifact: Value, channel: &str) -> Value {
        json!({
            "schema": SCHEMA,
            "channel": channel,
            "publishedAtMs": 1_730_000_000_000_i64,
            "revokedVersions": [],
            "releases": [{
                "version": "0.2.0",
                "notes": "测试",
                "artifacts": [artifact],
            }],
        })
    }

    fn parse(json: Value) -> Result<Manifest, String> {
        parse_manifest(json.to_string().as_bytes(), "stable")
    }

    #[test]
    fn test_good_manifest_parses_and_normalizes() {
        let m = parse(manifest_json(artifact_value(), "stable")).unwrap();
        assert_eq!(m.channel, "stable");
        assert_eq!(m.releases.len(), 1);
        assert_eq!(m.releases[0].version, "0.2.0");
        assert_eq!(m.releases[0].artifacts[0].sha256, "a".repeat(64));
        assert_eq!(m.releases[0].artifacts[0].publisher_id, "tongtian-official");
        assert_eq!(m.releases[0].artifacts[0].signature.len(), 64);
    }

    #[test]
    fn test_unknown_field_is_rejected_not_ignored() {
        // 这条是 D-12 的关键防线：未知键默默放过 = 将来新增的约束被旧版本读成「没这条要求」
        let mut raw = manifest_json(artifact_value(), "stable");
        raw["releases"][0]["artifacts"][0]["signaturePolicy"] = json!("optional");
        let err = parse(raw).unwrap_err();
        assert!(err.starts_with("UPDATE_INVALID_MANIFEST:"), "{err}");
        assert!(err.contains("signaturePolicy"), "{err}");

        let mut top = manifest_json(artifact_value(), "stable");
        top["allowUnsigned"] = json!(true);
        assert!(parse(top).unwrap_err().starts_with("UPDATE_INVALID_MANIFEST:"));
    }

    #[test]
    fn test_channel_and_schema_mismatch_are_refused() {
        // 渠道串台：把 beta 清单当 stable 用必须是错，不是「反正都是清单」
        let err = parse(manifest_json(artifact_value(), "beta")).unwrap_err();
        assert!(err.contains("渠道"), "{err}");
        assert!(err.contains("beta"), "{err}");

        let mut raw = manifest_json(artifact_value(), "stable");
        raw["schema"] = json!("tt-app-update/2");
        assert!(parse(raw).unwrap_err().contains("清单版本"));
    }

    #[test]
    fn test_sample_manifest_is_parseable() {
        // 样例与代码同源：样例读不进来就是文档在骗人
        let m = parse_manifest(sample_manifest_json().as_bytes(), "stable")
            .expect("样例清单必须能被自己的解析器读进来");
        assert_eq!(m.releases.len(), 1);
        assert_eq!(m.revoked_versions, vec!["0.1.5".to_string()]);
    }

    #[test]
    fn test_strict_field_grammar() {
        let cases: Vec<(&str, Value)> = vec![
            ("sha256 太短", { let mut a = artifact_value(); a["sha256"] = json!("ab"); a }),
            ("sha256 非十六进制", { let mut a = artifact_value(); a["sha256"] = json!("z".repeat(64)); a }),
            ("大小太小", { let mut a = artifact_value(); a["sizeBytes"] = json!(10_u64); a }),
            ("大小超上限", { let mut a = artifact_value(); a["sizeBytes"] = json!(MAX_ARTIFACT_BYTES + 1); a }),
            ("不支持的产物类型", { let mut a = artifact_value(); a["kind"] = json!("exe"); a }),
            ("不支持的平台", { let mut a = artifact_value(); a["platform"] = json!("ios"); a }),
            ("不支持的架构", { let mut a = artifact_value(); a["arch"] = json!("x86"); a }),
            ("签名不是 base64", { let mut a = artifact_value(); a["signature"] = json!("!!!"); a }),
            ("签名长度不对", { let mut a = artifact_value(); a["signature"] = json!("A".repeat(43)); a }),
            ("地址不是 URL", { let mut a = artifact_value(); a["url"] = json!("update.example.invalid/a.exe"); a }),
            ("地址带凭据", { let mut a = artifact_value(); a["url"] = json!("https://u:p@example.invalid/a.exe"); a }),
            ("发布者标识非法", { let mut a = artifact_value(); a["publisherId"] = json!("官方"); a }),
            ("没有产物", { json!({"version":"0.2.0","notes":"","artifacts":[]}) }),
        ];
        for (name, artifact) in cases {
            let mut raw = manifest_json(artifact_value(), "stable");
            if artifact.get("artifacts").is_some() {
                raw["releases"] = json!([artifact]);
            } else {
                raw["releases"][0]["artifacts"] = json!([artifact]);
            }
            let err = parse(raw).unwrap_err();
            assert!(
                err.starts_with("UPDATE_INVALID_MANIFEST:"),
                "{name} 应当整条拒掉，实为 {err}"
            );
        }
    }

    #[test]
    fn test_duplicate_release_and_duplicate_artifact_refused() {
        let mut raw = manifest_json(artifact_value(), "stable");
        raw["releases"].as_array_mut().unwrap().push(json!({
            "version": "0.2.0", "notes": "重复", "artifacts": [artifact_value()]
        }));
        assert!(parse(raw).unwrap_err().contains("两次"));

        let mut raw = manifest_json(artifact_value(), "stable");
        raw["releases"][0]["artifacts"]
            .as_array_mut()
            .unwrap()
            .push(artifact_value());
        assert!(parse(raw).unwrap_err().contains("两个产物"));
    }

    #[test]
    fn test_control_characters_in_notes_are_flattened() {
        let mut raw = manifest_json(artifact_value(), "stable");
        raw["releases"][0]["notes"] = json!("正常\u{7}告警\u{0}文本");
        let m = parse(raw).unwrap();
        let notes = &m.releases[0].notes;
        assert!(notes.contains("正常") && notes.contains("告警"), "{notes}");
        assert!(!notes.chars().any(|c| c == '\u{7}' || c == '\u{0}'), "{notes:?}");
    }

    #[test]
    fn test_version_grammar_and_ordering() {
        assert!(Version::parse("01.2.3").is_err(), "前导零必须拒");
        assert!(Version::parse("1.2").is_err());
        assert!(Version::parse("1.2.3.4").is_err());
        assert!(Version::parse("1.2..3").is_err());
        assert_eq!(Version::parse("v1.2.3").unwrap().raw, "1.2.3");

        let v = |s: &str| Version::parse(s).unwrap();
        assert!(v("0.2.0").is_newer_than(&v("0.1.9")));
        assert!(v("1.0.0").is_newer_than(&v("0.99.99")));
        assert!(!v("0.1.0").is_newer_than(&v("0.1.0")));
        assert!(!v("0.1.0-alpha").is_newer_than(&v("0.1.0")), "预发布必须低于同号正式版");
        assert!(v("0.1.1-alpha").is_newer_than(&v("0.1.0")));
        assert!(v("1.999999999.0").is_newer_than(&v("1.999999998.9")));
        assert!(Version::parse("9999999999.0.0").is_err(), "9 位以上不接");
        // 大小写归一：清单写 ALPHA 与 alpha 必须是同一个版本
        assert_eq!(v("1.0.0-ALPHA").raw, v("1.0.0-alpha").raw);
    }

    #[test]
    fn test_signing_payload_covers_every_deciding_field() {
        let payload = String::from_utf8(signing_payload(
            "stable", "0.2.0", "windows", "x64", "nsis", "official", "ab",
        ))
        .unwrap();
        assert!(payload.starts_with("tt-app-update-v1\n"), "{payload}");
        for part in ["stable", "0.2.0", "windows", "x64", "nsis", "official", "ab"] {
            assert!(payload.contains(part), "{part} 必须进签名载荷");
        }
        // 与市场域不同名：一份签名不能两边通用
        assert_ne!(SIGNING_DOMAIN, "tt-market-v1");
        assert!(!payload.contains("notes"), "文案不进签名载荷");
    }

    #[test]
    fn test_verify_artifact_with_real_signature() {
        let (pair, public) = keypair();
        let sha = sha256_hex(b"installer-bytes");
        let payload = signing_payload(
            "stable",
            "0.2.0",
            host_platform(),
            host_arch(),
            KIND_NSIS,
            "tongtian-official",
            &sha,
        );
        let signature =
            base64::engine::general_purpose::STANDARD_NO_PAD.encode(pair.sign(&payload).as_ref());

        let mut artifact = artifact_value();
        artifact["platform"] = json!(host_platform());
        artifact["arch"] = json!(host_arch());
        artifact["sha256"] = json!(sha);
        artifact["signature"] = json!(signature);
        let m = parse(manifest_json(artifact, "stable")).unwrap();
        let release = &m.releases[0];
        let art = &release.artifacts[0];

        // 没有锚点：如实说「本机还没有钥匙」，而不是「签名不对」
        let err = verify_artifact(&m, release, art, &[]).unwrap_err();
        assert!(err.starts_with("UPDATE_NO_TRUST_ANCHOR:"), "{err}");

        let anchor = Anchor::from_raw("tongtian-official", &public, "local").unwrap();
        let hit = verify_artifact(&m, release, art, &[anchor.clone()]).unwrap();
        assert_eq!(hit.public_key, anchor.public_key);
        assert_eq!(hit.source, "local");

        // 另一个发布者的钥匙：说「没信任」，不说「签错了」
        let (_other_pair, other_public) = keypair();
        let stranger = Anchor::from_raw("someone-else", &other_public, "local").unwrap();
        let err = verify_artifact(&m, release, art, &[stranger]).unwrap_err();
        assert!(err.starts_with("UPDATE_UNTRUSTED_PUBLISHER:"), "{err}");
        assert!(err.contains("tongtian-official"), "{err}");

        // 同标识换钥匙：验不过就是验不过，不会退化成「试完所有钥匙再猜」
        let rotated = Anchor::from_raw("tongtian-official", &other_public, "local").unwrap();
        let err = verify_artifact(&m, release, art, &[rotated]).unwrap_err();
        assert!(err.starts_with("UPDATE_SIGNATURE_INVALID:"), "{err}");

        // 载荷被改一个字段（哈希换掉）：旧签名立刻失效
        let mut tampered = art.clone();
        tampered.sha256 = "f".repeat(64);
        let err = verify_artifact(&m, release, &tampered, &[anchor.clone()]).unwrap_err();
        assert!(err.starts_with("UPDATE_SIGNATURE_INVALID:"), "{err}");

        // 市场域的签名不能拿来当更新域的用
        let market_payload = format!("tt-market-v1\n{}\n", String::from_utf8_lossy(&payload));
        let as_update = base64::engine::general_purpose::STANDARD_NO_PAD
            .encode(pair.sign(market_payload.as_bytes()).as_ref());
        let mut cross = art.clone();
        cross.signature = decode_base64(&as_update).unwrap();
        assert!(verify_artifact(&m, release, &cross, &[anchor]).is_err(), "跨域签名必须验不过");
    }

    #[test]
    fn test_anchor_key_must_be_32_bytes() {
        assert!(Anchor::from_raw("p", &"A".repeat(86), "local").is_err(), "非 32 字节的钥匙必须拒");
        let err = Anchor::from_raw("p", "!!!", "local").unwrap_err();
        assert!(err.starts_with("UPDATE_INVALID_PUBLISHER_KEY:"), "{err}");
        let a = Anchor::from_raw("P-1", &base64::engine::general_purpose::STANDARD.encode([7u8; 32]), "local").unwrap();
        assert_eq!(a.publisher_id, "p-1", "发布者标识要归一小写");
        assert_eq!(a.fingerprint, sha256_hex(&[7u8; 32]));
        assert_eq!(short_fingerprint(&a.fingerprint).chars().count(), 17, "展示指纹 = 16 位 + 省略号");
    }

    fn signed_artifact(pair: &ring::signature::Ed25519KeyPair, version: &str, seed: &str) -> Value {
        let sha = sha256_hex(seed.as_bytes());
        let payload = signing_payload(
            "stable",
            version,
            host_platform(),
            host_arch(),
            KIND_NSIS,
            "tongtian-official",
            &sha,
        );
        json!({
            "platform": host_platform(),
            "arch": host_arch(),
            "kind": KIND_NSIS,
            "url": "https://update.example.invalid/a.exe",
            "sha256": sha,
            "sizeBytes": 4096_u64,
            "publisherId": "tongtian-official",
            "signature": base64::engine::general_purpose::STANDARD_NO_PAD.encode(pair.sign(&payload).as_ref()),
        })
    }

    #[test]
    fn test_evaluate_refuses_downgrade_revoke_and_min_version() {
        let (pair, _public) = keypair();
        let raw = json!({
            "schema": SCHEMA,
            "channel": "stable",
            "publishedAtMs": 1_730_000_000_000_i64,
            "revokedVersions": ["0.3.0"],
            "releases": [
                {"version": "0.0.9", "artifacts": [signed_artifact(&pair, "0.0.9", "old")], "notes": "旧版"},
                {"version": "0.3.0", "artifacts": [signed_artifact(&pair, "0.3.0", "revoked")], "notes": "被撤销"},
                {"version": "0.4.0", "minAppVersion": "9.9.9", "artifacts": [signed_artifact(&pair, "0.4.0", "min")], "notes": "要求太高"},
                {"version": "0.5.0", "artifacts": [signed_artifact(&pair, "0.5.0", "best")], "notes": "可用"},
            ],
        });
        let m = parse(raw).unwrap();
        let verdict = evaluate(&m, "0.1.0");
        let rows = verdict["rows"].as_array().unwrap();
        assert_eq!(rows.len(), 4);
        assert_eq!(rows[0]["installable"], json!(false), "不比本机新的不能装");
        assert!(rows[0]["reasons"].to_string().contains("降级"));
        assert_eq!(rows[1]["installable"], json!(false), "被撤销的不能装");
        assert_eq!(rows[2]["installable"], json!(false), "minAppVersion 不满足的不能装");
        assert_eq!(rows[3]["installable"], json!(true));
        assert_eq!(verdict["installable"]["version"], json!("0.5.0"));
        assert!(verdict["notices"].to_string().contains("撤销了 1 个"), "{:?}", verdict["notices"]);

        // 本机版本本身被撤销时额外提醒一次
        let verdict = evaluate(&m, "0.3.0");
        assert!(verdict["notices"].to_string().contains("已被清单撤销"));
        assert_eq!(verdict["host"]["platform"], json!(host_platform()));
    }

    #[test]
    fn test_evaluate_picks_newest_not_last_row() {
        // 「取最新」不能等于「取最后一行」：清单按旧→新排是常态，倒着排也要挑对
        let (pair, _public) = keypair();
        let raw = json!({
            "schema": SCHEMA,
            "channel": "stable",
            "publishedAtMs": 1_730_000_000_000_i64,
            "revokedVersions": [],
            "releases": [
                {"version": "0.9.0", "artifacts": [signed_artifact(&pair, "0.9.0", "nine")]},
                {"version": "0.7.0", "artifacts": [signed_artifact(&pair, "0.7.0", "seven")]},
            ],
        });
        let verdict = evaluate(&parse(raw).unwrap(), "0.6.0");
        assert_eq!(verdict["installable"]["version"], json!("0.9.0"));
    }

    #[test]
    fn test_evaluate_with_unreadable_current_version_says_so() {
        let (pair, _public) = keypair();
        let raw = manifest_json(signed_artifact(&pair, "0.2.0", "x"), "stable");
        let verdict = evaluate(&parse(raw).unwrap(), "不是版本号");
        assert!(verdict["installable"].is_null());
        assert_eq!(verdict["notices"].as_array().unwrap().len(), 1);
        assert!(verdict["rows"].as_array().unwrap().is_empty());
    }

    #[test]
    fn test_url_fragment_stripped_and_scheme_table() {
        // https / http 都能进模型（网络侧再判回环与公网），fragment 一律丢掉
        let mut a = artifact_value();
        a["url"] = json!("http://127.0.0.1:8123/a.exe#seg=1");
        let m = parse(manifest_json(a, "stable")).unwrap();
        assert!(!m.releases[0].artifacts[0].url.contains('#'));
        assert!(m.releases[0].artifacts[0].url.starts_with("http://127.0.0.1:8123/"));

        let mut a = artifact_value();
        a["url"] = json!("ftp://example.invalid/a.exe");
        assert!(parse(manifest_json(a, "stable")).unwrap_err().contains("http / https"));
    }

    #[test]
    fn test_host_tables_match_kinds() {
        assert_eq!(kind_extension(KIND_NSIS), Some("exe"));
        assert_eq!(kind_extension(KIND_MSI), Some("msi"));
        assert_eq!(kind_extension("zip"), None);
        assert!(PLATFORMS.contains(&host_platform()), "本机平台必须在表里");
        assert!(ARCHES.contains(&host_arch()), "本机架构必须在表里：{}", host_arch());
    }
}
