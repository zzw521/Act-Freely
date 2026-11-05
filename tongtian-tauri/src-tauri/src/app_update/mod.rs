//! 应用自身的更新回路（缺陷 D-12 无自动更新渠道，#159）。
//!
//! 走的是 exec 车道（`exec:update_*`），所以本模块不需要打开 `lib.rs` 的命令清单，
//! 也不需要 `src/shared/contracts.ts`——通道名与响应形状都在 `contracts_exec.ts` 里。
//!
//! ## 为什么是自建而不是 `tauri-plugin-updater`
//!
//! Tauri 的 updater 要求三样本仓库今天都没有的东西：一条服务端发布端点、一把内容签名
//! 私钥、以及 `tauri.conf.json` 里的 `plugins.updater` 配置（`tongtian-server/app/desktop_index.py`
//! 自己就写着内容签名钥缺失）。引一个跑不通的插件依赖，等于把「无自动更新渠道」换成
//! 「有个装不上的插件」。所以这里自建**清单与校验**这一半，安装包本身仍是官方产物
//! （NSIS `installMode: currentUser` 与 MSI 同一个 `upgradeCode`），替换动作交给安装器——
//! 应用不去改写自己的 exe，那是安装器的工作，也是失败时唯一还能回头的地方。
//!
//! ## 流程（每一步都可能停，停了就如实说停在哪）
//!
//! ```text
//! 配置渠道 → 取清单 → 校验格式 → 逐产物验签 → 选一条本机可用的
//!          → 下载（限大小）→ 比对 sha256 → 原子改名进暂存区
//!          → 装前再算一次哈希 → 生成安装命令 → 用户确认后启动安装器
//!          → 下次启动结算：版本变了=成功；没变=失败，且给出回滚判定
//! ```
//!
//! ## 四条口径
//!
//! - **验签先于下载**：清单里的签名覆盖「渠道 + 版本 + 平台 + 架构 + 类型 + 发布者 + 哈希」，
//!   所以不必先下一个 100 MB 的包才知道它不合法。
//! - **本机锚点为空时一切自动路径都装不上**（今天内嵌锚点就是空的，见 `EMBEDDED_ANCHORS`），
//!   这是「无更新渠道时界面不放假按钮」的实现面：宁可不给按钮，也不给一个点了必然失败的按钮。
//! - **降级只走回滚**：清单里比当前版本旧的条目一律不装；回滚只认本机暂存区里留存的
//!   安装包，不重新取清单——否则被攻破的清单能把本机「更新」到带漏洞的旧版。
//! - **应用不自己退出**：安装器启动后由用户退出应用。当前版本没换过来就是失败，
//!   结算时如实写 `install-failed`，不猜「大概是装好了」。

use std::collections::BTreeSet;
use std::io::Write as _;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

/// 流式哈希要用 `Digest` 的 `update()`；只用它的构造与 `finalize`，命名成 `_` 避免污染作用域。
use sha2::Digest as _;

use crate::bridge::event_store::now_ms;

pub mod manifest;

use manifest::{
    artifact_view, decode_base64, evaluate, host_arch, host_platform, kind_extension,
    parse_manifest, short_fingerprint, sha256_hex, verify_artifact, Anchor, Artifact, Manifest,
    ARCHES, KINDS, MAX_ARTIFACT_BYTES, PLATFORMS, SCHEMA,
};

/// exec 车道里属于本模块的方法前缀（`ipc/exec.rs` 按它分流）。
pub const METHOD_PREFIX: &str = "update_";

pub const DIRNAME: &str = "update";
pub const STAGING_DIRNAME: &str = "staging";
const CHANNEL_FILENAME: &str = "channel.json";
const TRUST_FILENAME: &str = "trusted_publishers.json";
const JOURNAL_FILENAME: &str = "journal.json";
const LEDGER_FILENAME: &str = "ledger.jsonl";

/// 安装包文件名的产品段：与 `tauri.conf.json` 的 `mainBinaryName` 无关，
/// 只是暂存区里用来认「这是本应用的包」的标记。
pub const PRODUCT_TOKEN: &str = "renwoxing";

pub const DEFAULT_CHANNEL: &str = "stable";
pub const MANIFEST_MAX_BYTES: usize = 256 * 1024;
pub const MAX_STAGED_FILES: usize = 3;
pub const MAX_LEDGER_LINES: usize = 500;
pub const MAX_TRUSTED_KEYS: usize = 50;
pub const CONFIRM_WORD: &str = "UPDATE";
pub const IMPORT_CONFIRM_WORD: &str = "LOCAL-PACKAGE";

const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
const MANIFEST_TIMEOUT: Duration = Duration::from_secs(30);
const DOWNLOAD_TIMEOUT: Duration = Duration::from_secs(30 * 60);
const MAX_URL_LEN: usize = 2048;

/// 状态机（`journal.state`）：界面与自测都按这几个值说话，不许再发明第六个。
pub const STATE_EMPTY: &str = "empty";
pub const STATE_STAGED: &str = "staged";
pub const STATE_INSTALL_PENDING: &str = "install-pending";
pub const STATE_INSTALLED: &str = "installed";
pub const STATE_ROLLED_BACK: &str = "rolled-back";
pub const STATE_INSTALL_FAILED: &str = "install-failed";
pub const STATES: &[&str] = &[
    STATE_EMPTY,
    STATE_STAGED,
    STATE_INSTALL_PENDING,
    STATE_INSTALLED,
    STATE_ROLLED_BACK,
    STATE_INSTALL_FAILED,
];

pub const ACTION_UPGRADE: &str = "upgrade";
pub const ACTION_ROLLBACK: &str = "rollback";
pub const ORIGIN_MANIFEST: &str = "manifest";
pub const ORIGIN_LOCAL: &str = "local-import";

/// 锚点来源只有两个值：`embedded` 随包编译进来、本机删不掉；`local` 用户自己加的、可删。
pub const SOURCE_EMBEDDED: &str = "embedded";
pub const SOURCE_LOCAL: &str = "local";

/// 随包编译进来的信任锚点：`(发布者标识, base64 公钥)`。
///
/// **今天是空的**，而且这不是忘了填：本仓库还没有发布用的 Ed25519 私钥
/// （服务端 `desktop_index.py` 的注释同样承认内容签名钥缺失）。等有钥匙那天，
/// 这里加的必须是**公钥**，私钥只在发布机上；在那之前所有自动更新路径都会以
/// `UPDATE_NO_TRUST_ANCHOR` 如实拒绝，用户手工添加公钥或手工导入本地包才有下一步。
pub const EMBEDDED_ANCHORS: &[(&str, &str)] = &[];

// ── 路径（数据根只经 `file_perm::data_root()` 一处取，不许自己读环境变量）──

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Paths {
    pub root: PathBuf,
}

impl Paths {
    /// 生产入口：数据根下的 `update/`。
    pub fn from_data_root() -> Paths {
        Paths {
            root: crate::security::file_perm::data_root().join(DIRNAME),
        }
    }

    pub fn for_root(root: PathBuf) -> Paths {
        Paths { root }
    }

    pub fn staging_dir(&self) -> PathBuf {
        self.root.join(STAGING_DIRNAME)
    }

    pub fn channel_file(&self) -> PathBuf {
        self.root.join(CHANNEL_FILENAME)
    }

    pub fn trust_file(&self) -> PathBuf {
        self.root.join(TRUST_FILENAME)
    }

    pub fn journal_file(&self) -> PathBuf {
        self.root.join(JOURNAL_FILENAME)
    }

    pub fn ledger_file(&self) -> PathBuf {
        self.root.join(LEDGER_FILENAME)
    }

    pub fn staged_file(&self, file_name: &str) -> PathBuf {
        self.staging_dir().join(file_name)
    }

    fn ensure(&self) -> Result<(), String> {
        std::fs::create_dir_all(self.staging_dir())
            .map_err(|e| format!("UPDATE_STAGE_FAILED: 更新暂存目录建不出来（{e}）"))
    }
}

// ── 读写小工具 ──────────────────────────────────────────────────

fn move_aside(file: &Path) -> String {
    // 坏文件改名留证，不覆盖、不删：读不懂的清单/账本要能拿去问「到底写成什么样了」。
    let name = file
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| "更新文件".to_string());
    let (stem, ext) = match name.split_once('.') {
        Some((a, b)) => (a.to_string(), format!(".{b}")),
        None => (name.clone(), String::new()),
    };
    let target = file.with_file_name(format!("{stem}.corrupt-{}{ext}", now_ms()));
    match std::fs::rename(file, &target) {
        Ok(()) => target.file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or_default(),
        Err(_) => format!("改名失败·原文件仍在 {name}"),
    }
}

/// 读一份 JSON 存储：不存在 = `None`；坏 JSON = 改名留证后当没有，并把原因带回给界面。
///
/// 这里绝不「静默按默认值继续」——按默认值继续意味着用户配好的渠道地址被一次磁盘抖动
/// 抹掉后，界面显示的是「未配置」，而他会以为是自己没配过。
fn load_store<T: serde::de::DeserializeOwned>(file: &Path, what: &str) -> (Option<T>, String) {
    match std::fs::read(file) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => (None, String::new()),
        Err(e) => (None, format!("{what}读不出来（{e}）")),
        Ok(bytes) => match serde_json::from_slice::<T>(&bytes) {
            Ok(value) => (Some(value), String::new()),
            Err(e) => (
                None,
                format!("{what}不是合法 JSON（{e}），已改名留证：{moved}", moved = move_aside(file)),
            ),
        },
    }
}

fn write_atomic(file: &Path, text: &str) -> Result<(), String> {
    if let Some(parent) = file.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("UPDATE_STAGE_FAILED: 目录建不出来（{e}）"))?;
    }
    let tmp = file.with_extension(format!("tmp-{}", now_ms()));
    let write = (|| -> Result<(), std::io::Error> {
        let mut f = std::fs::File::create(&tmp)?;
        f.write_all(text.as_bytes())?;
        f.sync_data()?;
        Ok(())
    })();
    write.map_err(|e| format!("UPDATE_STAGE_FAILED: 写 {file:?} 失败（{e}）"))?;
    if file.exists() {
        let _ = std::fs::remove_file(file);
    }
    std::fs::rename(&tmp, file).map_err(|e| {
        let _ = std::fs::remove_file(&tmp);
        format!("UPDATE_STAGE_FAILED: 改名 {file:?} 失败（{e}）")
    })
}

fn err_code(message: &str) -> String {
    message.split_once(": ").map(|(c, _)| c.to_string()).unwrap_or_default()
}

// ── 渠道配置 ────────────────────────────────────────────────────

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct Channel {
    pub enabled: bool,
    pub url: String,
    pub channel: String,
    pub updated_at_ms: i64,
}

fn invalid_url(detail: &str) -> String {
    format!("UPDATE_INVALID_URL: {detail}")
}

fn host_is_loopback(url: &reqwest::Url) -> bool {
    let Some(host) = url.host_str() else {
        return false;
    };
    if host.eq_ignore_ascii_case("localhost") {
        return true;
    }
    let bare = host.strip_prefix('[').and_then(|h| h.strip_suffix(']')).unwrap_or(host);
    matches!(bare.parse::<IpAddr>(), Ok(ip) if ip.is_loopback())
}

fn bare_host(host: &str) -> &str {
    host.strip_prefix('[')
        .and_then(|rest| rest.strip_suffix(']'))
        .unwrap_or(host)
}

fn host_label(url: &reqwest::Url) -> String {
    url.host_str().unwrap_or("未知主机").to_string()
}

/// 回环之外的地址必须是公网：更新源不是内网服务，指到内网就是被做了或写错了。
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

/// 渠道地址校验：远程只许 HTTPS，本机回环许 HTTP（自建/联调源）。
/// 与市场的同一族规则各写一份——market 是别的窗口的领地，伸手改就是踩线。
fn normalize_channel_url(raw: &str) -> Result<(String, bool), String> {
    if raw.len() > MAX_URL_LEN {
        return Err(invalid_url("地址过长"));
    }
    let mut url = reqwest::Url::parse(raw)
        .map_err(|_| invalid_url("无法解析该地址，请填写完整的 https URL"))?;
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
        "http" => return Err(invalid_url(
            "更新渠道必须使用 HTTPS；本机联调可用 http://127.0.0.1 或 http://localhost",
        )),
        _ => return Err(invalid_url("只支持 http / https 地址")),
    }
    Ok((url.to_string(), local))
}

fn read_channel(paths: &Paths) -> (Channel, String) {
    let (value, note) = load_store::<Channel>(&paths.channel_file(), "更新渠道配置");
    match value {
        Some(mut c) => {
            c.url = c.url.trim().to_string();
            if c.channel.trim().is_empty() {
                c.channel = DEFAULT_CHANNEL.to_string();
            }
            if c.url.is_empty() {
                c.enabled = false;
            }
            (c, note)
        }
        None => (
            Channel {
                enabled: false,
                url: String::new(),
                channel: DEFAULT_CHANNEL.to_string(),
                updated_at_ms: 0,
            },
            note,
        ),
    }
}

fn channel_view(channel: &Channel, note: &str) -> Value {
    json!({
        "configured": !channel.url.is_empty(),
        "enabled": channel.enabled && !channel.url.is_empty(),
        "url": channel.url,
        "channel": channel.channel,
        "updatedAtMs": channel.updated_at_ms,
        "loadNote": note,
    })
}

/// 保存/清除更新渠道。
///
/// 这一步**只写本机文件，不发网络请求**，所以回话里不许出现「可达」「已连通」这类
/// 由这次调用推断出来的说法（早先版本这里挂过一个 `probe.reachableNow`，它是拿
/// 「字符串非空」冒充连通性核对的，删了）。真要验证地址通不通，用 `update_check`。
fn set_channel(paths: &Paths, payload: Option<&Value>) -> Result<Value, String> {
    let raw_url = request_field(payload, "url");
    let channel_name = {
        let c = request_field(payload, "channel");
        if c.is_empty() {
            DEFAULT_CHANNEL.to_string()
        } else {
            c
        }
    };
    // 渠道名与清单里的 `channel` 走同一个校验与归一化：本机存 `Stable`、清单写 `stable`
    // 这种大小写差异，过去会让取清单永远对不上，而且对不上的理由是「渠道不匹配」，很误导。
    let channel_name = manifest::require_channel(&channel_name).map_err(bad_channel_name)?;
    let enabled = payload
        .and_then(|p| p.get("enabled"))
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    // 空串 = 清除渠道：「关掉但还存着地址」这件事由 enabled 表达
    let url = if raw_url.is_empty() {
        String::new()
    } else {
        normalize_channel_url(&raw_url)?.0
    };
    if enabled && url.is_empty() {
        return Err(invalid_url("要启用渠道就必须给出地址"));
    }
    let stored = Channel {
        enabled,
        url: url.clone(),
        channel: channel_name,
        updated_at_ms: now_ms() as i64,
    };
    write_atomic(
        &paths.channel_file(),
        &serde_json::to_string_pretty(&stored)
            .map_err(|e| format!("UPDATE_STAGE_FAILED: 渠道配置写不出（{e}）"))?,
    )?;
    record(paths, "channel", true, "", &format!("渠道={} url={url}", stored.channel));
    Ok(json!({
        "ok": true,
        "channel": channel_view(&stored, ""),
        "note": if url.is_empty() { "已清除更新渠道；本机回到「没有更新渠道」的状态" } else { "已保存渠道；这一步只把地址写进本机，没有发起网络请求，也没有核对过它能不能连通" },
    }))
}

// ── 信任锚点 ────────────────────────────────────────────────────

/// 本机信任文件里的一行。**只存标识和公钥两件事**：
/// 指纹和来源每次读盘都重算一遍（见 `read_local_anchors`），不信任文件里写好的成品。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
struct TrustEntry {
    publisher_id: String,
    public_key: String,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
struct TrustFile {
    anchors: Vec<TrustEntry>,
}

/// 读本机信任列表。不合规则的记录直接丢掉并在 note 里说明条数——
/// 一个被改坏的信任文件不该让整个更新回路报错，也不该被照着采信。
fn read_local_anchors(paths: &Paths) -> (Vec<Anchor>, String) {
    let (value, note) = load_store::<TrustFile>(&paths.trust_file(), "更新信任公钥");
    let mut out: Vec<Anchor> = Vec::new();
    let mut dropped = 0usize;
    for entry in value.map(|file| file.anchors).unwrap_or_default() {
        match Anchor::from_raw(&entry.publisher_id, &entry.public_key, SOURCE_LOCAL) {
            Ok(anchor) if !out.iter().any(|a| a.publisher_id == anchor.publisher_id) => out.push(anchor),
            Ok(_) => {}
            Err(_) => dropped += 1,
        }
    }
    let note = if dropped == 0 {
        note
    } else {
        format!("{note}；另有 {dropped} 条本机信任记录不合法，已忽略")
    };
    (out, note)
}

fn trust_entries_json(anchors: &[Anchor]) -> String {
    let entries: Vec<TrustEntry> = anchors
        .iter()
        .map(|a| TrustEntry { publisher_id: a.publisher_id.clone(), public_key: a.public_key.clone() })
        .collect();
    json!({ "anchors": entries }).to_string()
}

fn embedded_anchors() -> Vec<Anchor> {
    EMBEDDED_ANCHORS
        .iter()
        .filter_map(|(id, key)| Anchor::from_raw(id, key, SOURCE_EMBEDDED).ok())
        .collect()
}

/// 验签用的锚点集合 = 内嵌 ∪ 本机添加。内嵌那份永远在内（`update_trust_remove` 删不掉）。
fn anchors(paths: &Paths) -> (Vec<Anchor>, String) {
    let (local, note) = read_local_anchors(paths);
    let mut out = embedded_anchors();
    for a in local {
        if !out.iter().any(|e| e.publisher_id == a.publisher_id && e.public_key == a.public_key) {
            out.push(a);
        }
    }
    (out, note)
}

fn anchor_view(a: &Anchor) -> Value {
    json!({
        "publisherId": a.publisher_id,
        "publicKey": a.public_key,
        "fingerprint": a.fingerprint,
        "fingerprintShort": short_fingerprint(&a.fingerprint),
        "source": a.source,
        "removable": a.source == SOURCE_LOCAL,
    })
}

fn add_anchor(paths: &Paths, publisher_id: &str, public_key: &str) -> Result<Value, String> {
    let candidate = Anchor::from_raw(publisher_id, public_key, SOURCE_LOCAL)?;
    let embedded = embedded_anchors();
    if let Some(existing) = embedded.iter().find(|a| a.publisher_id == candidate.publisher_id) {
        // 同标识换钥必须拒：否则「加一把同名的钥匙」就能把官方锚点的判定绕过去。
        if existing.public_key != candidate.public_key {
            return Err(format!(
                "UPDATE_PUBLISHER_KEY_CONFLICT: 发布者「{}」已有随包内嵌公钥（指纹 {}），本机不能再加一把同标识的不同公钥",
                candidate.publisher_id,
                short_fingerprint(&existing.fingerprint)
            ));
        }
        return Ok(json!({
            "ok": true,
            "added": false,
            "anchor": anchor_view(existing),
            "note": "这把就是随包内嵌的锚点，不必再加，也删不掉",
        }));
    }
    let (mut local, _) = read_local_anchors(paths);
    if let Some(existing) = local.iter().find(|a| a.publisher_id == candidate.publisher_id) {
        if existing.public_key == candidate.public_key {
            return Ok(json!({
                "ok": true,
                "added": false,
                "anchor": anchor_view(existing),
                "note": "本机已经信任这把公钥，没有重复写入",
            }));
        }
        return Err(format!(
            "UPDATE_PUBLISHER_KEY_CONFLICT: 发布者「{}」本机已信任指纹 {}；换钥请先删除旧信任再添加",
            candidate.publisher_id,
            short_fingerprint(&existing.fingerprint)
        ));
    }
    if local.len() >= MAX_TRUSTED_KEYS {
        return Err(format!(
            "UPDATE_STAGE_FAILED: 本机信任公钥已达 {MAX_TRUSTED_KEYS} 条上限，先删再加"
        ));
    }
    local.push(candidate.clone());
    write_atomic(&paths.trust_file(), &trust_entries_json(&local))?;
    Ok(json!({
        "ok": true,
        "added": true,
        "anchor": anchor_view(&candidate),
        "note": "已写入本机信任列表；这把钥匙能签的更新清单都会被接受",
    }))
}

fn remove_anchor(paths: &Paths, publisher_id: &str) -> Result<Value, String> {
    let id = publisher_id.trim().to_ascii_lowercase();
    if embedded_anchors().iter().any(|a| a.publisher_id == id) {
        return Ok(json!({
            "ok": false,
            "removed": false,
            "reason": "随包内嵌的锚点删不掉：删它等于把出厂信任也关掉，只能由新版本带回替代钥匙",
        }));
    }
    let (mut local, _) = read_local_anchors(paths);
    let before = local.len();
    local.retain(|a| a.publisher_id != id);
    if local.len() == before {
        return Ok(json!({
            "ok": false,
            "removed": false,
            "reason": format!("本机信任列表里没有发布者「{id}」"),
        }));
    }
    write_atomic(&paths.trust_file(), &trust_entries_json(&local))?;
    Ok(json!({
        "ok": true,
        "removed": true,
        "publisherId": id,
        "remaining": local.len(),
        "note": "已删除；用这把钥匙签的清单之后会验不过",
    }))
}

// ── 状态账本 ────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct Staged {
    pub version: String,
    pub file: String,
    pub sha256: String,
    pub size_bytes: u64,
    pub kind: String,
    pub platform: String,
    pub arch: String,
    pub publisher_id: String,
    pub signed: bool,
    pub origin: String,
    pub staged_at_ms: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct Pending {
    pub version: String,
    pub file: String,
    pub action: String,
    pub previous_version: String,
    pub launched_at_ms: i64,
}

/// 本机留存的、装的就是当前版本的安装包——回滚只认它。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct Retained {
    pub version: String,
    pub file: String,
    pub sha256: String,
    pub size_bytes: u64,
    pub kind: String,
    pub kept_at_ms: i64,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", default)]
pub struct Journal {
    pub schema: String,
    pub state: String,
    pub staged: Option<Staged>,
    pub pending: Option<Pending>,
    pub rollback: Option<Retained>,
    pub last_error: String,
    pub note: String,
    pub updated_at_ms: i64,
}

impl Journal {
    fn fresh() -> Journal {
        Journal {
            schema: SCHEMA.to_string(),
            state: STATE_EMPTY.to_string(),
            ..Default::default()
        }
    }
}

fn read_journal(paths: &Paths) -> (Journal, String) {
    let (value, note) = load_store::<Journal>(&paths.journal_file(), "更新状态账本");
    match value {
        Some(mut j) => {
            if j.schema != SCHEMA {
                // 别的 schema 的账本不猜：改名留证，从空账本开始（暂存区的文件还在，能重认）
                let moved = move_aside(&paths.journal_file());
                (
                    Journal::fresh(),
                    format!("更新状态账本的格式版本是「{}」，本机只认「{SCHEMA}」，已改名留证：{moved}", j.schema),
                )
            } else {
                if j.state.is_empty() {
                    j.state = STATE_EMPTY.to_string();
                }
                (j, note)
            }
        }
        None => (Journal::fresh(), note),
    }
}

fn write_journal(paths: &Paths, journal: &Journal) -> Result<(), String> {
    write_atomic(
        &paths.journal_file(),
        &serde_json::to_string_pretty(journal)
            .map_err(|e| format!("UPDATE_STAGE_FAILED: 状态账本写不出（{e}）"))?,
    )
}

/// 每次入口都先结算，再把结果落账；返回「这次有没有改变状态」。生产路径用本机版本号。
pub fn settle(paths: &Paths) -> Result<bool, String> {
    settle_with(paths, &app_version())
}

/// 结算的实现面：`installed` 由调用方给。
///
/// 把「本机版本号」做成参数不是为了绕开检查——判定本身是纯函数（版本号变没变、
/// 变得对不对），做成参数才谈得上在测试里演「装上了」和「没装上」两条分支，
/// 而不必去改 `CARGO_PKG_VERSION`（编译期常量，改不了）或环境变量（跨模块共享、会互相打架）。
///
/// 结算只看一件事：**本机版本号变没变**。版本号来自 `CARGO_PKG_VERSION`，
/// 装成功就会跟着新包变，所以它是本机唯一拿得到的「装没装上」的证据。
/// 引擎版本（`engine/server.py` 的 `ENGINE_VERSION`）与它不是一回事，绝不参与比较。
pub fn settle_with(paths: &Paths, installed: &str) -> Result<bool, String> {
    let (mut journal, _) = read_journal(paths);
    let Some(pending) = journal.pending.clone() else {
        return Ok(false);
    };
    let changed = installed != pending.previous_version;
    let hit = installed == pending.version;
    journal.pending = None;
    journal.updated_at_ms = now_ms() as i64;
    if hit {
        journal.state = if pending.action == ACTION_ROLLBACK {
            STATE_ROLLED_BACK.to_string()
        } else {
            STATE_INSTALLED.to_string()
        };
        journal.staged = None;
        journal.note = if pending.action == ACTION_ROLLBACK {
            format!("回滚已生效：本机版本现在是 {installed}")
        } else {
            format!("更新已生效：本机版本从 {} 变成 {installed}", pending.previous_version)
        };
        journal.last_error = String::new();
        // 回滚材料只在「升级成功」那一刻采集，认的是**上一个版本**的包（`previous_version`）。
        // 回滚本身成功之后不再留材料：那份包已经就是当前版本，留着只会诱导出一次空回滚。
        journal.rollback = if pending.action == ACTION_ROLLBACK {
            None
        } else {
            find_package_for_version(paths, &pending.previous_version, &journal)
        };
    } else if changed {
        // 版本变了但不是清单说的那个：装错了包/装了别的东西，如实报，不替用户解释
        journal.state = STATE_INSTALL_FAILED.to_string();
        journal.last_error = format!(
            "本机版本现在是 {installed}，与本次{}目标 {} 不一致；已停在结算，不再自动动手",
            if pending.action == ACTION_ROLLBACK { "回滚" } else { "更新" },
            pending.version
        );
        journal.note = "结算发现版本对不上目标，请人工核对安装的是哪一份".to_string();
    } else {
        journal.state = STATE_INSTALL_FAILED.to_string();
        journal.last_error = format!(
            "安装器已启动过（{} 毫秒前）但本机版本仍是 {installed}：多半是没退出应用、被 UAC 拒了或安装器中途失败",
            now_ms() as i64 - pending.launched_at_ms
        );
        journal.note = "可以重试安装，也可以导入本地包；暂存区的包没有删".to_string();
    }
    // 失败分支**不动** `rollback`：判定材料是上一次成功结算留下的，安装失败不该把退路一起清掉。
    write_journal(paths, &journal)?;
    Ok(true)
}

/// 在暂存区里找「装的就是 `<version>`」的那一份安装包，按**内容**认。
///
/// 名字里的 `hash12` 是自己编的也不算，所以找到候选后读回来重算一次 sha256：
/// 账本里有这条记录（`staged`/`rollback` 任一行指向同一个文件）时比完整哈希，
/// 没有时只能退而要求完整哈希以名字里那 12 位开头——这是文件名与内容之间唯一可用的绑定。
/// 账本由调用方传入：结算已经在内存里拿着一份，不该每找一个文件再读一次盘。
fn find_package_for_version(paths: &Paths, version: &str, journal: &Journal) -> Option<Retained> {
    if version.is_empty() {
        return None;
    }
    let declared: Vec<Staged> = [
        journal.staged.clone(),
        journal.rollback.as_ref().map(|r| Staged {
            version: r.version.clone(),
            file: r.file.clone(),
            sha256: r.sha256.clone(),
            size_bytes: r.size_bytes,
            kind: r.kind.clone(),
            platform: String::new(),
            arch: String::new(),
            publisher_id: String::new(),
            signed: false,
            origin: String::new(),
            staged_at_ms: r.kept_at_ms,
        }),
    ]
    .into_iter()
    .flatten()
    .collect();
    let entries = std::fs::read_dir(paths.staging_dir()).ok()?;
    let mut candidates: Vec<StagedName> = Vec::new();
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        let parsed = parse_staged_name(&name)?;
        if parsed.version == version {
            candidates.push(parsed);
        }
    }
    candidates.sort_by(|a, b| a.file_name().cmp(&b.file_name()));
    for parsed in candidates {
        let file = parsed.file_name();
        let bytes = match std::fs::read(paths.staged_file(&file)) {
            Ok(b) => b,
            Err(_) => continue,
        };
        if bytes.len() < manifest::MIN_ARTIFACT_BYTES as usize {
            continue;
        }
        let digest = sha256_hex(&bytes);
        let exact = declared.iter().find(|s| s.file == file);
        let ok = match exact {
            Some(s) => digest == s.sha256,
            None => digest.starts_with(&parsed.hash12),
        };
        if !ok {
            continue;
        }
        let kind = kind_of_extension(&parsed.extension).unwrap_or_else(|| match exact {
            Some(s) => s.kind.clone(),
            None => manifest::KIND_NSIS.to_string(),
        });
        return Some(Retained {
            version: parsed.version.clone(),
            file,
            sha256: digest,
            size_bytes: bytes.len() as u64,
            kind,
            kept_at_ms: now_ms() as i64,
        });
    }
    None
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StagedName {
    pub product: String,
    pub version: String,
    pub platform: String,
    pub arch: String,
    pub hash12: String,
    pub extension: String,
}

impl StagedName {
    /// 反回它自己的文件名：`parse_staged_name` 认出来的名字必须能原样拼回去，
    /// 否则「按内容找某一版本的包」这条路径就没有可靠的文件名可用（版本串里不许有 `-`，
    /// 所以这段拆分是无歧义的）。
    pub fn file_name(&self) -> String {
        format!(
            "{}-{}-{}-{}-{}.{}",
            self.product, self.version, self.platform, self.arch, self.hash12, self.extension
        )
    }
}

/// 扩展名反查类型（`kind_extension` 的反向）：只用于「账本没了、只能靠文件名认」的那一支。
pub fn kind_of_extension(extension: &str) -> Option<String> {
    [manifest::KIND_NSIS, manifest::KIND_MSI]
        .into_iter()
        .find(|kind| kind_extension(kind) == Some(extension))
        .map(str::to_string)
}

/// 暂存文件名的唯一形状：`<product>-<version>-<platform>-<arch>-<hash12>.<ext>`。
///
/// 版本与哈希进文件名是有意的：光看目录就知道「这一份是哪个版本的哪一次下载」，
/// 账本丢了也能重新认；解析不出来的一律当外来文件，不参与判定也不误删。
pub fn staged_file_name(version: &str, platform: &str, arch: &str, sha256: &str, kind: &str) -> String {
    format!(
        "{PRODUCT_TOKEN}-{version}-{platform}-{arch}-{}.{}",
        &sha256[..sha256.len().min(12)],
        kind_extension(kind).unwrap_or("bin")
    )
}

/// 反解暂存文件名。
///
/// 版本串**可以带 `-`**（预发布写法 `1.2.3-beta.1`，见 `manifest::Version::parse`），
/// 所以不能简单按 `-` 切成五段——那样预发布的包永远认不出来，只能当外来文件被清理掉。
/// 可靠的做法是从两头认：`product` 固定在第一段，`hash12/arch/platform` 固定在最后三段，
/// 中间全部算版本号。认不出的形状一律返回 `None`（外来文件不参与判定，也不误删）。
pub fn parse_staged_name(name: &str) -> Option<StagedName> {
    let (stem, extension) = name.rsplit_once('.')?;
    if stem.is_empty() || extension.is_empty() || !extension.bytes().all(|b| b.is_ascii_alphanumeric()) {
        return None;
    }
    let parts: Vec<&str> = stem.split('-').collect();
    if parts.len() < 5 {
        return None;
    }
    if parts[0] != PRODUCT_TOKEN {
        return None;
    }
    let hash12 = parts[parts.len() - 1];
    let arch = parts[parts.len() - 2];
    let platform = parts[parts.len() - 3];
    let version = parts[1..parts.len() - 3].join("-");
    if hash12.len() != 12
        || !hash12.bytes().all(|b| b.is_ascii_hexdigit())
        || hash12 != hash12.to_ascii_lowercase()
        || !PLATFORMS.contains(&platform)
        || !ARCHES.contains(&arch)
    {
        return None;
    }
    // 版本号必须是**规范形**：能解析、且解析后的规范串与文件里写的逐字一致。
    // 不这样要求的话，同一个版本可以有 `V1.2.3` 与 `1.2.3` 两份文件，回滚按名字找就会找到错的那份。
    let parsed_version = manifest::Version::parse(&version).ok()?;
    if parsed_version.raw != version {
        return None;
    }
    Some(StagedName {
        product: parts[0].to_string(),
        version,
        platform: platform.to_string(),
        arch: arch.to_string(),
        hash12: hash12.to_string(),
        extension: extension.to_string(),
    })
}

fn list_staged(paths: &Paths) -> Vec<Value> {
    let Ok(entries) = std::fs::read_dir(paths.staging_dir()) else {
        return Vec::new();
    };
    // 账本只读一次：它落在盘上，逐条文件都去读一遍既慢，又会在读的过程中重复触发「坏账本改名留证」
    let referenced: BTreeSet<String> = {
        let (journal, _) = read_journal(paths);
        [
            journal.staged.as_ref().map(|s| s.file.clone()),
            journal.pending.as_ref().map(|p| p.file.clone()),
            journal.rollback.as_ref().map(|r| r.file.clone()),
        ]
        .into_iter()
        .flatten()
        .collect()
    };
    let mut out: Vec<Value> = Vec::new();
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        let meta = match entry.metadata() {
            Ok(m) => m,
            Err(_) => continue,
        };
        let parsed = parse_staged_name(&name);
        out.push(json!({
            "file": name,
            "bytes": meta.len(),
            "recognized": parsed.is_some(),
            "version": parsed.as_ref().map(|p| p.version.clone()),
            "platform": parsed.as_ref().map(|p| p.platform.clone()),
            "arch": parsed.as_ref().map(|p| p.arch.clone()),
            "hashPrefix": parsed.as_ref().map(|p| p.hash12.clone()),
            "referencedByJournal": referenced.contains(&name),
        }));
    }
    out.sort_by(|a, b| a["file"].as_str().unwrap_or_default().cmp(b["file"].as_str().unwrap_or_default()));
    out
}

/// 只删「认得出是本应用的包、且账本不再引用」的文件：外来文件一律不动。
fn prune_staging(paths: &Paths) -> Vec<String> {
    let (journal, _) = read_journal(paths);
    let kept: BTreeSet<String> = [
        journal.staged.as_ref().map(|s| s.file.clone()),
        journal.pending.as_ref().map(|p| p.file.clone()),
        journal.rollback.as_ref().map(|r| r.file.clone()),
    ]
    .into_iter()
    .flatten()
    .collect();
    let mut candidates: Vec<(String, std::fs::Metadata)> = Vec::new();
    let Ok(entries) = std::fs::read_dir(paths.staging_dir()) else {
        return Vec::new();
    };
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        if kept.contains(&name) || parse_staged_name(&name).is_none() {
            continue;
        }
        if let Ok(meta) = entry.metadata() {
            candidates.push((name, meta));
        }
    }
    // 按修改时间从旧到新排；留够上限就一个也不删
    candidates.sort_by_key(|(_, meta)| {
        meta.modified()
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_millis() as i64)
            .unwrap_or(i64::MIN)
    });
    let droppable = candidates.len().saturating_sub(MAX_STAGED_FILES);
    let mut removed = Vec::new();
    for (name, _) in candidates.into_iter().take(droppable) {
        if std::fs::remove_file(paths.staged_file(&name)).is_ok() {
            removed.push(name);
        }
    }
    removed
}

// ── 留痕 ────────────────────────────────────────────────────────

/// 本车道自己的动作留痕（`ledger.jsonl`）。
///
/// 为什么不进 `audit_logs`：exec 车道的分派是纯函数（`ipc/exec.rs` 没有 `State<Store>`），
/// 要写审计就得给车道命令加状态参数，那会同时动到 `lib.rs` 的挂载面与其它六条车道的形状。
/// 更新动作低频、且每一条都能在文件里核对，所以先落在数据根里，
/// 「并进统一审计」是 D-12 剩下的那一半（见台账的未做项）。
fn record(paths: &Paths, action: &str, ok: bool, code: &str, detail: &str) {
    let line = json!({
        "atMs": now_ms() as i64,
        "action": action,
        "ok": ok,
        "code": code,
        "detail": detail,
    })
    .to_string();
    if std::fs::create_dir_all(&paths.root).is_err() {
        return;
    }
    let file = paths.ledger_file();
    let mut text = std::fs::read_to_string(&file).unwrap_or_default();
    text.push_str(&line);
    text.push('\n');
    // 上限截断只留最新的那些条：留痕太长会把真正有用的最近几次挤掉
    let lines: Vec<&str> = text.lines().collect();
    if lines.len() > MAX_LEDGER_LINES {
        let keep = &lines[lines.len() - MAX_LEDGER_LINES..];
        let _ = std::fs::write(&file, format!("{}\n", keep.join("\n")));
    } else {
        let _ = std::fs::write(&file, text);
    }
}

fn read_ledger(paths: &Paths, limit: usize) -> (Vec<Value>, usize, usize) {
    let text = std::fs::read_to_string(paths.ledger_file()).unwrap_or_default();
    let mut bad = 0usize;
    let mut rows: Vec<Value> = Vec::new();
    for line in text.lines().filter(|l| !l.trim().is_empty()) {
        match serde_json::from_str::<Value>(line) {
            Ok(v) => rows.push(v),
            Err(_) => bad += 1,
        }
    }
    // 截断前先记下真实条数：界面要说「共 N 条，这里只给最近 M 条」，不能让人以为只有 M 条。
    let total = rows.len();
    let limit = limit.clamp(1, MAX_LEDGER_LINES);
    if rows.len() > limit {
        rows = rows.split_off(rows.len() - limit);
    }
    rows.reverse();
    (rows, bad, total)
}

// ── 网络 ────────────────────────────────────────────────────────

fn build_client(local: bool) -> Result<reqwest::Client, String> {
    let builder = reqwest::Client::builder()
        // 302 会把请求带到没校验过的主机；更新源应当直接填最终地址
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(CONNECT_TIMEOUT)
        .user_agent(concat!("RenWoXing-Update/", env!("CARGO_PKG_VERSION")));
    let builder = if local {
        builder.no_proxy()
    } else {
        crate::proxy::current().apply(builder)?
    };
    builder
        .build()
        .map_err(|e| format!("UPDATE_FETCH_FAILED: 无法创建网络客户端（{e}）"))
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

fn fetch_failed(detail: &str, url: &reqwest::Url) -> String {
    format!("UPDATE_FETCH_FAILED: {detail}（{0}）", host_label(url))
}

/// 解析主机并确认全是公网地址；回环只在 `local_allowed` 时放行。
async fn ensure_target_allowed(url: &reqwest::Url, local_allowed: bool) -> Result<(), String> {
    if host_is_loopback(url) {
        return if local_allowed {
            Ok(())
        } else {
            Err(invalid_url("更新地址指向本机，已拒绝"))
        };
    }
    let host = url
        .host_str()
        .ok_or_else(|| invalid_url("更新地址缺少主机名"))?;
    let host = bare_host(host);
    if let Ok(ip) = host.parse::<IpAddr>() {
        return if ip_is_public(ip) {
            Ok(())
        } else {
            Err(invalid_url("更新地址是内网或保留地址，已拒绝"))
        };
    }
    let port = url.port_or_known_default().unwrap_or(443);
    let addrs: Vec<SocketAddr> = tokio::net::lookup_host((host, port))
        .await
        .map_err(|_| fetch_failed("无法解析更新主机名，请检查网络或 DNS 设置", url))?
        .collect();
    if addrs.is_empty() {
        return Err(fetch_failed("更新主机没有可用的解析结果", url));
    }
    if addrs.iter().any(|addr| !ip_is_public(addr.ip())) {
        return Err(invalid_url("更新主机解析到内网或保留地址，已拒绝"));
    }
    Ok(())
}

async fn send_get(url: &reqwest::Url, local: bool, timeout: Duration) -> Result<reqwest::Response, String> {
    let client = build_client(local)?;
    let resp = client
        .get(url.clone())
        .timeout(timeout)
        .send()
        .await
        .map_err(|e| fetch_failed(network_reason(&e), url))?;
    let status = resp.status();
    if status.is_redirection() {
        return Err(format!(
            "UPDATE_HTTP_ERROR: 更新源返回重定向（HTTP {}）；为避免跳到未经校验的地址已拒绝，请直接填写最终地址",
            status.as_u16()
        ));
    }
    if !status.is_success() {
        return Err(format!(
            "UPDATE_HTTP_ERROR: 更新源返回 HTTP {}（{}）",
            status.as_u16(),
            host_label(url)
        ));
    }
    Ok(resp)
}

/// 取清单：先按上限流式读，超限直接拒。
async fn fetch_manifest_bytes(url: &str, local: bool) -> Result<Vec<u8>, String> {
    let parsed = reqwest::Url::parse(url).map_err(|_| invalid_url("渠道地址不是完整 URL"))?;
    ensure_target_allowed(&parsed, local).await?;
    let resp = send_get(&parsed, local, MANIFEST_TIMEOUT).await?;
    if let Some(len) = resp.content_length() {
        if len > MANIFEST_MAX_BYTES as u64 {
            return Err(format!(
                "UPDATE_TOO_LARGE: 更新清单约 {}KB，超过 {MAX_MANIFEST_MAX_KB}KB 上限",
                len / 1024,
                MAX_MANIFEST_MAX_KB = MANIFEST_MAX_BYTES / 1024
            ));
        }
    }
    let mut resp = resp;
    let mut out: Vec<u8> = Vec::new();
    while let Some(chunk) = resp
        .chunk()
        .await
        .map_err(|e| fetch_failed(network_reason(&e), &parsed))?
    {
        if out.len().saturating_add(chunk.len()) > MANIFEST_MAX_BYTES {
            return Err(format!(
                "UPDATE_TOO_LARGE: 更新清单超过 {}KB 上限",
                MANIFEST_MAX_BYTES / 1024
            ));
        }
        out.extend_from_slice(&chunk);
    }
    if out.is_empty() {
        return Err("UPDATE_INVALID_MANIFEST: 更新源返回了空内容".to_string());
    }
    Ok(out)
}

/// 下载安装包：边下边写、边算哈希，两处大小上限（声明值与硬上限）都守。
///
/// 落盘走「临时文件 → 改名」：下了一半断电或哈希不对，正式位置不会出现半成品。
async fn download_to_staging(
    paths: &Paths,
    url: &str,
    expected_sha: &str,
    declared_bytes: u64,
    local: bool,
) -> Result<(PathBuf, u64), String> {
    let parsed = reqwest::Url::parse(url).map_err(|_| invalid_url("产物下载地址不是完整 URL"))?;
    ensure_target_allowed(&parsed, local).await?;
    if declared_bytes > MAX_ARTIFACT_BYTES {
        return Err(format!(
            "UPDATE_TOO_LARGE: 清单声明的产物大小超过 {MAX_ARTIFACT_BYTES} 字节硬上限"
        ));
    }
    paths.ensure()?;
    let tmp = paths
        .staging_dir()
        .join(format!(".downloading-{}.part", now_ms()));
    let mut file = std::fs::File::create(&tmp)
        .map_err(|e| format!("UPDATE_STAGE_FAILED: 暂存临时文件建不出来（{e}）"))?;
    let mut hasher = <sha2::Sha256 as sha2::Digest>::new();
    let mut written: u64 = 0;
    let result: Result<(), String> = async {
        let mut resp = send_get(&parsed, local, DOWNLOAD_TIMEOUT).await?;
        if let Some(len) = resp.content_length() {
            if len > declared_bytes {
                return Err(format!(
                    "UPDATE_TOO_LARGE: 实际返回 {len} 字节，超过清单声明的 {declared_bytes} 字节"
                ));
            }
        }
        while let Some(chunk) = resp
            .chunk()
            .await
            .map_err(|e| fetch_failed(network_reason(&e), &parsed))?
        {
            if written.saturating_add(chunk.len() as u64) > declared_bytes {
                return Err(format!(
                    "UPDATE_TOO_LARGE: 下载字节数超过清单声明的 {declared_bytes} 字节，已中断"
                ));
            }
            hasher.update(&chunk);
            file.write_all(&chunk)
                .map_err(|e| format!("UPDATE_STAGE_FAILED: 写暂存文件失败（{e}）"))?;
            written += chunk.len() as u64;
        }
        Ok(())
    }
    .await;
    if let Err(e) = result {
        drop(file);
        let _ = std::fs::remove_file(&tmp);
        return Err(e);
    }
    let _ = file.sync_all();
    drop(file);
    let digest = sha2::Digest::finalize(hasher);
    let actual = digest.iter().map(|b| format!("{b:02x}")).collect::<String>();
    if actual != expected_sha {
        let _ = std::fs::remove_file(&tmp);
        return Err(format!(
            "UPDATE_HASH_MISMATCH: 期望 {}…，实际 {}…（安装包与清单声明不一致，已删除这份下载）",
            &expected_sha[..expected_sha.len().min(12)],
            &actual[..actual.len().min(12)]
        ));
    }
    if written == 0 {
        let _ = std::fs::remove_file(&tmp);
        return Err("UPDATE_HASH_MISMATCH: 下载结果为空文件，已删除".to_string());
    }
    Ok((tmp, written))
}

fn rename_into_place(tmp: &Path, dest: &Path) -> Result<(), String> {
    if dest.exists() {
        // 同名旧包先删掉再改名：Windows 上「替换正在被引用的文件」会失败，删了更干净
        let _ = std::fs::remove_file(dest);
    }
    std::fs::rename(tmp, dest).map_err(|e| {
        let _ = std::fs::remove_file(tmp);
        format!("UPDATE_STAGE_FAILED: 暂存改名失败（{e}）")
    })
}

/// 装之前再算一次哈希：下载完成到安装之间隔着用户的几次点击，
/// 这段时间里文件被人换掉是本机唯一还没守住的窗口。
fn verify_staged_file(paths: &Paths, sha256: &str, file: &str) -> Result<u64, String> {
    let path = paths.staged_file(file);
    let bytes = std::fs::read(&path)
        .map_err(|_| format!("UPDATE_STAGE_FAILED: 暂存的安装包不在了（{file}），请重新下载"))?;
    let actual = sha256_hex(&bytes);
    if actual != sha256 {
        return Err(format!(
            "UPDATE_HASH_MISMATCH: 暂存文件的哈希已变成 {}…，与账本记录的 {}… 不符，已拒绝安装",
            &actual[..12],
            &sha256[..sha256.len().min(12)]
        ));
    }
    Ok(bytes.len() as u64)
}

// ── 安装命令 ────────────────────────────────────────────────────

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct InstallPlan {
    pub program: String,
    pub args: Vec<String>,
    pub elevation_note: String,
    pub quit_note: String,
    pub action: String,
}

/// 只生成命令，不执行：`update_install_plan` 与 `update_rollback_plan` 用它把
/// 「将要跑什么」摊给用户看，真正执行的 `update_install` 走的是同一份计划。
pub fn install_plan(kind: &str, path: &Path, action: &str) -> Result<InstallPlan, String> {
    if !cfg!(windows) {
        return Err(format!(
            "UPDATE_UNSUPPORTED_ARTIFACT: 本机不是 Windows，没有可执行 {kind} 安装器的路径"
        ));
    }
    let (program, args, elevation_note) = match kind {
        manifest::KIND_NSIS => (
            path.to_string_lossy().to_string(),
            vec!["/S".to_string()],
            "NSIS 安装器按 currentUser 模式安装，通常不需要管理员权限".to_string(),
        ),
        manifest::KIND_MSI => (
            "msiexec".to_string(),
            vec![
                "/i".to_string(),
                path.to_string_lossy().to_string(),
                "/quiet".to_string(),
                "/norestart".to_string(),
            ],
            "MSI 按机器安装会弹 UAC；本机升级用的是同一个 upgradeCode"
                .to_string(),
        ),
        other => {
            return Err(format!(
                "UPDATE_UNSUPPORTED_ARTIFACT: 产物类型「{other}」没有已知的静默安装命令"
            ))
        }
    };
    Ok(InstallPlan {
        program,
        args,
        elevation_note,
        quit_note: "安装器不会替你先退出应用；正在运行的程序会让覆盖安装失败，请先手动退出再确认安装".to_string(),
        action: action.to_string(),
    })
}

fn spawn_plan(plan: &InstallPlan) -> Result<u32, String> {
    let mut command = std::process::Command::new(&plan.program);
    command.args(&plan.args);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // 不弹新控制台窗口（GUI 子系统里 spawn 会带出一个黑框）
        command.creation_flags(0x0800_0000);
    }
    let child = command
        .spawn()
        .map_err(|e| format!("UPDATE_INSTALL_FAILED: 安装器启动失败（{e}）"))?;
    Ok(child.id())
}

// ── 对外的动作 ──────────────────────────────────────────────────

pub fn app_version() -> String {
    env!("CARGO_PKG_VERSION").to_string()
}

/// 取清单并做一次本机判定；不做任何写入。
/// `check_core` 的两种结局：业务拒绝（带回给界面的那一包）与「清单已取到并已判定」。
enum Checked {
    Refusal(Value),
    Ready(Manifest, Channel, Value),
}

/// 取清单 → 验格式 → 判定形状 → 逐本机产物验签，**只做一次**。
///
/// 为什么要把「看」和「下」合成一步而不是下载时再取一次清单：分两次取，界面看到的
/// 与实际用来选包的可能不是同一份（渠道在中间被换过、服务端换了发布），
/// 那正是「按 A 的清单决定、按 B 的清单下载」这类事故的形状。
async fn check_core(paths: &Paths) -> Result<Checked, String> {
    let (channel, _) = read_channel(paths);
    if channel.url.is_empty() {
        // 「没有渠道」是本机的正常状态，不是服务故障：回 ok=false + 理由，界面照实显示空态。
        // 报 Err 会让车道把它记成一次失败留痕，也会诱导出界面「重试一下」的假按钮。
        return Ok(Checked::Refusal(json!({
            "ok": false,
            "reason": "还没有配置更新渠道：本机没有可信的地址，也没有发起任何网络请求",
            "channel": channel_view(&channel, ""),
            "note": "设置里的更新分区仍未接上（D-12 未做项），可以先用 update_channel_set 把地址写进本机",
        })));
    }
    if !channel.enabled {
        return Ok(Checked::Refusal(json!({
            "ok": false,
            "reason": "更新渠道已保存但处于关闭状态，本次没有发起网络请求",
            "channel": channel_view(&channel, ""),
        })));
    }
    let (normalized, local) = normalize_channel_url(&channel.url)?;
    let bytes = fetch_manifest_bytes(&normalized, local).await?;
    let manifest = parse_manifest(&bytes, &channel.channel)?;
    let (anchors, anchor_note) = anchors(paths);
    let verdict = evaluate(&manifest, &app_version());
    let rows = verdict["rows"]
        .as_array()
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .map(|mut row| {
            // 只有形状判定已经认可「这条本机可装」的才补验签。
            // 反过来做会出大事：一个签名合法但**版本更旧/已被吊销**的条目，
            // 会因为「签是对的」被改回 installable=true，等于把降级洗白成可装。
            if row["installable"] == json!(true) {
                if let Some((release, art)) =
                    host_artifact_of(&manifest, row["version"].as_str().unwrap_or_default())
                {
                    row["signature"] = signature_row(&manifest, &release, &art, &anchors);
                    let signed = row["signature"]["ok"] == json!(true);
                    row["installable"] = json!(signed);
                    if !signed {
                        // 先把文案取出来再改 reasons：同一次遍历里对 `row` 既可变又可读，
                        // 直接写 `reasons.push(json!(row[...]))` 编译不过（借用冲突）。
                        let message = row["signature"]["message"].clone();
                        if let Some(reasons) = row["reasons"].as_array_mut() {
                            reasons.push(message);
                        }
                    }
                } else {
                    // evaluate 只给「本机有对应产物」的条目放行，所以这里理论上到不了；
                    // 到了说明两处判定不同源，宁可判不可装也不静默放过。
                    row["installable"] = json!(false);
                    if let Some(reasons) = row["reasons"].as_array_mut() {
                        reasons.push(json!("清单里没有本机平台/架构对应的产物，无法验签"));
                    }
                }
            }
            row
        })
        .collect::<Vec<_>>();
    let installable = rows
        .iter()
        .find(|r| r["installable"] == json!(true))
        .map(|r| json!({"version": r["version"], "artifact": r["artifact"]}));
    let mut notices = verdict["notices"].as_array().cloned().unwrap_or_default();
    if !anchor_note.is_empty() {
        notices.push(json!(anchor_note));
    }
    if anchors.is_empty() {
        notices.push(json!(
            "本机没有任何受信任的发布公钥：所有版本都无法验签，界面不要把「可更新」当成「能装」"
        ));
    }
    let view = json!({
        "ok": true,
        "channel": channel_view(&channel, ""),
        "fetchedAtMs": now_ms() as i64,
        "manifestBytes": bytes.len(),
        "manifestChannel": manifest.channel,
        "publishedAtMs": manifest.published_at_ms,
        "revokedVersions": manifest.revoked_versions,
        "currentVersion": app_version(),
        "rows": rows,
        "installable": installable,
        "notices": notices,
        "trustedAnchors": anchors.iter().map(anchor_view).collect::<Vec<_>>(),
        "note": "验签在这里做（还没下载）；下载后再比一次哈希，装之前再比一次",
    });
    Ok(Checked::Ready(manifest, channel, view))
}

/// 对外的「看一眼」：只回判定结果，清单留在内存里给下载路径复用（见 `check_core`）。
async fn check_once(paths: &Paths) -> Result<Value, String> {
    match check_core(paths).await? {
        Checked::Refusal(v) => Ok(v),
        Checked::Ready(_, _, v) => Ok(v),
    }
}

/// 按版本号在清单里找到「本机这一条」产物。
///
/// 形状判定（`manifest::evaluate`）与验签必须走同一次查找：两处各写一遍
/// 「平台 + 架构」的匹配规则，就会有一处先漏掉架构，出现「说可装但没验过签」的窗口。
fn host_artifact_of(manifest: &Manifest, version: &str) -> Option<(manifest::Release, Artifact)> {
    if version.is_empty() {
        return None;
    }
    let release = manifest.releases.iter().find(|r| r.version == version)?.clone();
    let art = release
        .artifacts
        .iter()
        .find(|a| a.platform == host_platform() && a.arch == host_arch())?
        .clone();
    Some((release, art))
}

fn signature_row(manifest: &Manifest, release: &manifest::Release, art: &Artifact, anchors: &[Anchor]) -> Value {
    match verify_artifact(manifest, release, art, anchors) {
        Ok(anchor) => json!({
            "ok": true,
            "publisherId": anchor.publisher_id,
            "fingerprintShort": short_fingerprint(&anchor.fingerprint),
            "source": anchor.source,
            "message": format!("签名已校验（发布者 {}，指纹 {}）", anchor.publisher_id, short_fingerprint(&anchor.fingerprint)),
        }),
        Err(e) => json!({
            "ok": false,
            "code": err_code(&e),
            "message": e,
        }),
    }
}

async fn stage_version(paths: &Paths, version: &str) -> Result<Value, String> {
    let (journal, _) = read_journal(paths);
    if journal.pending.is_some() {
        return Ok(json!({
            "ok": false,
            "reason": format!("上一次{}还没结算（目标版本 {}），先重启应用把状态认清楚",
                if journal.state == STATE_INSTALL_PENDING { "安装" } else { "动作" },
                journal.pending.map(|p| p.version).unwrap_or_default()),
        }));
    }
    let (manifest, _channel, checked) = match check_core(paths).await? {
        Checked::Refusal(v) => return Ok(v),
        Checked::Ready(manifest, channel, view) => (manifest, channel, view),
    };
    let row = checked["rows"]
        .as_array()
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .find(|r| r["version"].as_str() == Some(version));
    let Some(row) = row else {
        return Ok(json!({
            "ok": false,
            "reason": format!("清单里没有版本 {version} 这一条；不按需指定的版本去下别的包"),
            "currentVersion": app_version(),
            "available": checked["rows"].as_array().map(|rows| rows.iter().filter_map(|r| r["version"].as_str()).collect::<Vec<_>>()).unwrap_or_default(),
        }));
    };
    if row["installable"] != json!(true) {
        return Ok(json!({
            "ok": false,
            "reason": format!("{version} 这条本机不可装：{}", row["reasons"].as_array().map(|rs| rs.iter().filter_map(|v| v.as_str()).collect::<Vec<_>>().join("；")).unwrap_or_default()),
            "signature": row["signature"],
        }));
    }
    let (release, art) = host_artifact_of(&manifest, version)
        .ok_or_else(|| format!("UPDATE_INVALID_MANIFEST: 清单里没有 {version} 的本机产物"))?;
    let anchors = anchors(paths).0;
    let anchor = verify_artifact(&manifest, &release, &art, &anchors)?;
    let local = host_is_loopback_url(&art.url);
    let file_name = staged_file_name(&release.version, &art.platform, &art.arch, &art.sha256, &art.kind);
    let dest = paths.staged_file(&file_name);
    if dest.exists() && verify_staged_file(paths, &art.sha256, &file_name).is_ok() {
        record(paths, "download", true, "", &format!("{file_name} 已在暂存区且哈希一致，未重复下载"));
        let staged = Staged {
            version: release.version.clone(),
            file: file_name.clone(),
            sha256: art.sha256.clone(),
            size_bytes: art.size_bytes,
            kind: art.kind.clone(),
            platform: art.platform.clone(),
            arch: art.arch.clone(),
            publisher_id: art.publisher_id.clone(),
            signed: true,
            origin: ORIGIN_MANIFEST.to_string(),
            staged_at_ms: now_ms() as i64,
        };
        let mut journal = journal;
        journal.schema = SCHEMA.to_string();
        journal.state = STATE_STAGED.to_string();
        journal.staged = Some(staged.clone());
        journal.pending = None;
        journal.note = "暂存区已有同一份安装包（哈希一致），不必重新下载".to_string();
        journal.updated_at_ms = now_ms() as i64;
        write_journal(paths, &journal)?;
        return Ok(json!({
            "ok": true,
            "reused": true,
            "staged": staged_row(&staged),
            "signature": {"ok": true, "fingerprintShort": short_fingerprint(&anchor.fingerprint)},
            "journalState": STATE_STAGED,
            "note": "复用暂存区里那份哈希一致的包",
        }));
    }
    let (tmp, bytes) = download_to_staging(paths, &art.url, &art.sha256, art.size_bytes, local).await?;
    rename_into_place(&tmp, &dest)?;
    let staged = Staged {
        version: release.version.clone(),
        file: file_name.clone(),
        sha256: art.sha256.clone(),
        size_bytes: bytes,
        kind: art.kind.clone(),
        platform: art.platform.clone(),
        arch: art.arch.clone(),
        publisher_id: art.publisher_id.clone(),
        signed: true,
        origin: ORIGIN_MANIFEST.to_string(),
        staged_at_ms: now_ms() as i64,
    };
    let mut journal = journal;
    journal.schema = SCHEMA.to_string();
    journal.state = STATE_STAGED.to_string();
    journal.staged = Some(staged.clone());
    journal.pending = None;
    journal.note = format!("已下载并校验 {file_name}");
    journal.last_error = String::new();
    journal.updated_at_ms = now_ms() as i64;
    write_journal(paths, &journal)?;
    let removed = prune_staging(paths);
    record(paths, "download", true, "", &format!("{file_name} 落暂存区（{bytes} 字节），清理 {} 个旧包", removed.len()));
    Ok(json!({
        "ok": true,
        "reused": false,
        "staged": staged_row(&staged),
        "signature": {
            "ok": true,
            "publisherId": anchor.publisher_id,
            "fingerprintShort": short_fingerprint(&anchor.fingerprint),
        },
        "pruned": removed,
        "journalState": STATE_STAGED,
        "note": "哈希已核对；下一步看 update_install_plan 的命令，确认后才动手",
    }))
}

fn host_is_loopback_url(raw: &str) -> bool {
    reqwest::Url::parse(raw)
        .map(|u| host_is_loopback(&u))
        .unwrap_or(false)
}

fn staged_row(s: &Staged) -> Value {
    json!({
        "version": s.version,
        "file": s.file,
        "sha256": s.sha256,
        "sizeBytes": s.size_bytes,
        "kind": s.kind,
        "platform": s.platform,
        "arch": s.arch,
        "publisherId": s.publisher_id,
        "signed": s.signed,
        "origin": s.origin,
        "stagedAtMs": s.staged_at_ms,
    })
}

fn launch_staged(paths: &Paths, file: &str, version: &str, action: &str, confirm: &str) -> Result<Value, String> {
    if confirm != CONFIRM_WORD {
        let plan = install_plan_of(paths, file, version, action)
            .unwrap_or_else(|e| json!({ "ok": false, "reason": e }));
        return Ok(json!({
            "ok": false,
            "reason": format!("需要确认词 {CONFIRM_WORD} 才会启动安装器；本次没有动任何文件"),
            "confirmWord": CONFIRM_WORD,
            "plan": plan,
        }));
    }
    let (journal, _) = read_journal(paths);
    if journal.pending.is_some() {
        return Ok(json!({
            "ok": false,
            "reason": "已有一次安装在结算前等着，不能连着装第二次；先重启应用让本机认清楚现在是什么版本",
        }));
    }
    let staged = journal
        .staged
        .clone()
        .ok_or_else(|| "UPDATE_STAGE_FAILED: 本机还没有已校验的安装包，请先 update_download 或导入本地包".to_string())?;
    if action != ACTION_UPGRADE {
        return Err("UPDATE_INVALID_REQUEST: 本函数只走升级路径；回滚请用 update_rollback，它认的是账本里留存的那一份旧包".to_string());
    }
    if file != staged.file || version != staged.version {
        return Ok(json!({
            "ok": false,
            "reason": format!(
                "请求装的是 {version}/{file}，本机暂存的是 {} / {}；不按下别的包",
                staged.version, staged.file
            ),
        }));
    }
    // 来源闸：清单路径必须留下过验签记录；本地导入路径本来就是用户指名道姓给的包，
    // 它没有「发布者签名」这件事，但仍要过下面那道按内容的哈希复核。
    if !staged.signed && staged.origin != ORIGIN_LOCAL {
        return Ok(json!({
            "ok": false,
            "reason": "这一份包既没有验签记录也不是用户手工导入的，不启动安装器",
        }));
    }
    verify_staged_file(paths, &staged.sha256, &staged.file)?;
    let plan = install_plan(&staged.kind, &paths.staged_file(&staged.file), action)?;
    let mut journal = journal;
    journal.pending = Some(Pending {
        version: staged.version.clone(),
        file: staged.file.clone(),
        action: action.to_string(),
        previous_version: app_version(),
        launched_at_ms: now_ms() as i64,
    });
    journal.state = STATE_INSTALL_PENDING.to_string();
    journal.updated_at_ms = now_ms() as i64;
    // 先落账再启动：机器在两次动作中间断电，下次启动也能认出「装过但没结算」
    write_journal(paths, &journal)?;
    let pid = match spawn_plan(&plan) {
        Ok(pid) => pid,
        Err(e) => {
            journal.pending = None;
            journal.state = STATE_STAGED.to_string();
            journal.last_error = e.clone();
            journal.note = "安装器没能启动，状态已退回暂存".to_string();
            write_journal(paths, &journal)?;
            record(paths, action, false, &err_code(&e), &e);
            return Err(e);
        }
    };
    record(paths, action, true, "", &format!("{} 安装器已启动 pid={pid}", staged.file));
    Ok(json!({
        "ok": true,
        "launched": true,
        "pid": pid,
        "action": action,
        "fromVersion": app_version(),
        "toVersion": staged.version,
        "stagedFile": staged.file,
        "plan": plan,
        "journalState": STATE_INSTALL_PENDING,
        "note": format!("安装器已启动。本机不会自动退出：{0}。下次启动时自动结算，版本没变过来就如实记为安装失败", plan.quit_note),
    }))
}

/// 把「将要跑的那条命令」摊出来，不执行。请求必须指名道姓对上暂存里那一份，
/// 对不上是业务拒绝（`ok=false`），不是服务故障——界面要能原样把这句说给用户。
fn install_plan_of(paths: &Paths, file: &str, version: &str, action: &str) -> Result<Value, String> {
    let (journal, _) = read_journal(paths);
    let Some(staged) = journal.staged.clone() else {
        return Ok(json!({
            "ok": false,
            "reason": "本机还没有已校验的安装包，没有可给的计划；先 update_download（或 update_import_local）",
            "staged": null,
        }));
    };
    if staged.file != file || staged.version != version {
        return Ok(json!({
            "ok": false,
            "reason": format!(
                "暂存的是 {} / {}，与请求的 {version}/{file} 不一致；不给一份没核对过的命令",
                staged.version, staged.file
            ),
            "staged": staged_row(&staged),
        }));
    }
    let plan = install_plan(&staged.kind, &paths.staged_file(&staged.file), action)?;
    Ok(json!({
        "ok": true,
        "plan": plan,
        "staged": staged_row(&staged),
        "confirmWord": CONFIRM_WORD,
        "note": "计划只是命令，没有执行；执行要带确认词",
    }))
}

fn status_view(paths: &Paths) -> Result<Value, String> {
    let settled = settle(paths)?;
    let (journal, journal_note) = read_journal(paths);
    let (channel, channel_note) = read_channel(paths);
    let (anchor_list, anchor_note) = anchors(paths);
    let notes: Vec<String> = [journal_note, channel_note, anchor_note]
        .into_iter()
        .filter(|s| !s.is_empty())
        .collect();
    let state_known = STATES.contains(&journal.state.as_str());
    Ok(json!({
        "ok": true,
        "settledNow": settled,
        "appVersion": app_version(),
        "state": journal.state,
        "stateKnown": state_known,
        // 表外的状态原样回出来，但同时说明它不在闭合表里：界面据此决定不放任何动作按钮，
        // 而不是猜「大概是 staged 的近亲」——账本可能被外部工具改过，猜出来的按钮就是误操作
        "stateNotes": if state_known { String::new() } else { format!("账本里的状态「{}」不在本实现的闭合状态表里，界面不许按已知状态点亮按钮", journal.state) },
        "staged": journal.staged.as_ref().map(staged_row),
        "pending": journal.pending.as_ref().map(|p| json!({
            "version": p.version,
            "file": p.file,
            "action": p.action,
            "previousVersion": p.previous_version,
            "launchedAtMs": p.launched_at_ms,
        })),
        "rollback": journal.rollback.as_ref().map(retained_row),
        "rollbackAvailable": journal.rollback.is_some(),
        "lastError": journal.last_error,
        "note": journal.note,
        "loadNotes": notes,
        "channel": channel_view(&channel, ""),
        "trustedAnchors": anchor_list.iter().map(anchor_view).collect::<Vec<_>>(),
        "stagedFiles": list_staged(paths),
        "paths": {
            "dir": paths.root.display().to_string(),
            "staging": paths.staging_dir().display().to_string(),
            "journal": paths.journal_file().display().to_string(),
            "ledger": paths.ledger_file().display().to_string(),
        },
        "stagingNote": "暂存区里的包不会自动删；账本引用的三份之外的旧包会被清理",
    }))
}

fn import_local(paths: &Paths, file: &str, version: &str, confirm: &str) -> Result<Value, String> {
    if confirm != IMPORT_CONFIRM_WORD {
        return Ok(json!({
            "ok": false,
            "reason": format!(
                "导入本地包要把一份**没有清单、没有签名**的安装包变成可执行的东西，需要确认词 {IMPORT_CONFIRM_WORD}"
            ),
            "confirmWord": IMPORT_CONFIRM_WORD,
        }));
    }
    let version = version.trim();
    let version = manifest::Version::parse(version).map_err(|e| format!("UPDATE_INVALID_MANIFEST: 版本号为零时先说清这个包是哪个版本（{e}）"))?;
    let source = PathBuf::from(file.trim());
    if source.as_os_str().is_empty() {
        return Ok(json!({ "ok": false, "reason": "没有给出安装包路径" }));
    }
    let meta = std::fs::metadata(&source)
        .map_err(|_| format!("UPDATE_STAGE_FAILED: 读不到这个路径（{file}），请确认文件还在本机"))?;
    if !meta.is_file() {
        return Ok(json!({ "ok": false, "reason": "这个路径不是普通文件，不导入" }));
    }
    if meta.len() < manifest::MIN_ARTIFACT_BYTES || meta.len() > MAX_ARTIFACT_BYTES {
        return Ok(json!({
            "ok": false,
            "reason": format!(
                "这个文件 {} 字节，不在 {}—{} 字节之间，不像安装包",
                meta.len(),
                manifest::MIN_ARTIFACT_BYTES,
                MAX_ARTIFACT_BYTES
            ),
        }));
    }
    let extension = source
        .extension()
        .map(|e| e.to_string_lossy().to_ascii_lowercase())
        .unwrap_or_default();
    let kind = match extension.as_str() {
        "exe" => manifest::KIND_NSIS,
        "msi" => manifest::KIND_MSI,
        other => {
            return Ok(json!({
                "ok": false,
                "reason": format!("后缀「{other}」对应的产物类型没有已知的安装命令（只认 exe / msi）"),
            }))
        }
    };
    let bytes = std::fs::read(&source)
        .map_err(|e| format!("UPDATE_STAGE_FAILED: 读这个文件失败（{e}）"))?;
    let sha = sha256_hex(&bytes);
    paths.ensure()?;
    let file_name = staged_file_name(&version.raw, host_platform(), host_arch(), &sha, kind);
    let dest = paths.staged_file(&file_name);
    if !dest.exists() {
        std::fs::write(&dest, &bytes)
            .map_err(|e| format!("UPDATE_STAGE_FAILED: 复制进暂存区失败（{e}）"))?;
    }
    let staged = Staged {
        version: version.raw.clone(),
        file: file_name.clone(),
        sha256: sha.clone(),
        size_bytes: bytes.len() as u64,
        kind: kind.to_string(),
        platform: host_platform().to_string(),
        arch: host_arch().to_string(),
        publisher_id: String::new(),
        signed: false,
        origin: ORIGIN_LOCAL.to_string(),
        staged_at_ms: now_ms() as i64,
    };
    let (mut journal, _) = read_journal(paths);
    if journal.pending.is_some() {
        let _ = std::fs::remove_file(&dest);
        return Ok(json!({
            "ok": false,
            "reason": "上一次安装还没结算，先重启应用把状态认清楚",
        }));
    }
    journal.schema = SCHEMA.to_string();
    journal.state = STATE_STAGED.to_string();
    journal.staged = Some(staged.clone());
    journal.note = format!("已导入本地包 {file_name}（未经签名）");
    journal.last_error = String::new();
    journal.updated_at_ms = now_ms() as i64;
    write_journal(paths, &journal)?;
    let removed = prune_staging(paths);
    record(paths, "import", true, "", &format!("{file_name} 由本机导入，哈希 {sha}"));
    Ok(json!({
        "ok": true,
        "staged": staged_row(&staged),
        "pruned": removed,
        "note": "这一份没有清单也没有签名，能装是因为你亲手指名导入了它；装之前仍会再比一次哈希",
    }))
}

fn describe_view(paths: &Paths) -> Value {
    let (channel, channel_note) = read_channel(paths);
    let (anchor_list, _) = anchors(paths);
    json!({
        "ok": true,
        "schema": SCHEMA,
        "signingDomain": manifest::SIGNING_DOMAIN,
        "lane": "exec",
        "methodPrefix": METHOD_PREFIX,
        "methods": METHODS,
        "appVersion": app_version(),
        "engineVersionNote": "引擎版本（engine/server.py 的 ENGINE_VERSION）与本机版本不是一回事，不参与更新判定",
        "host": {"platform": host_platform(), "arch": host_arch()},
        "kinds": KINDS,
        "platforms": PLATFORMS,
        "arches": ARCHES,
        "versionRule": "主.次.补丁[-预发布]；段值 1—9 位十进制、不许前导零；预发布低于同号正式版",
        "confirmWord": CONFIRM_WORD,
        "importConfirmWord": IMPORT_CONFIRM_WORD,
        "limits": {
            "manifestBytes": MANIFEST_MAX_BYTES,
            "artifactBytesMax": MAX_ARTIFACT_BYTES,
            "artifactBytesMin": manifest::MIN_ARTIFACT_BYTES,
            "releases": manifest::MAX_RELEASES,
            "artifactsPerRelease": manifest::MAX_ARTIFACTS_PER_RELEASE,
            "revokedVersions": manifest::MAX_REVOKED_VERSIONS,
            "stagedFiles": MAX_STAGED_FILES,
            "ledgerLines": MAX_LEDGER_LINES,
            "trustedKeys": MAX_TRUSTED_KEYS,
        },
        "states": STATES,
        "embeddedAnchorCount": EMBEDDED_ANCHORS.len(),
        "trustedAnchors": anchor_list.iter().map(anchor_view).collect::<Vec<_>>(),
        "channel": channel_view(&channel, &channel_note),
        "paths": {
            "dir": paths.root.display().to_string(),
            "staging": paths.staging_dir().display().to_string(),
            "journal": paths.journal_file().display().to_string(),
            "ledger": paths.ledger_file().display().to_string(),
            "channel": paths.channel_file().display().to_string(),
            "trust": paths.trust_file().display().to_string(),
        },
        "sampleManifest": serde_json::from_str::<Value>(&manifest::sample_manifest_json())
            .unwrap_or_else(|_| json!({"note": "样例清单读不出来，这是实现缺陷"})),
        "signatureNote": "更新清单没有「可不签名」这一档：本机锚点为空时装不上任何东西，这是当前状态而不是开关",
        "rollbackNote": "回滚材料只在「升级成功结算」那一刻采集，认的是上一个版本留在暂存区的那一份包（按内容哈希复核）；回滚成功后不再留存，本机没有它就没有自动回滚，只给手动导入的路径",
    })
}

fn rollback_view(paths: &Paths, confirm: &str) -> Result<Value, String> {
    let (journal, _) = read_journal(paths);
    let Some(retained) = journal.rollback.clone() else {
        return Ok(json!({
            "ok": false,
            "reason": "本机没有可用于回滚的安装包：这台机器的上一个版本不是经这条回路装上的。可以走 update_import_local 手工导入上一个版本的包",
            "rollbackAvailable": false,
        }));
    };
    let plan = install_plan(&retained.kind, &paths.staged_file(&retained.file), ACTION_ROLLBACK)?;
    if confirm != CONFIRM_WORD {
        return Ok(json!({
            "ok": false,
            "reason": format!("回滚同样是覆盖安装，需要确认词 {CONFIRM_WORD}"),
            "confirmWord": CONFIRM_WORD,
            "plan": plan,
            "rollback": retained_row(&retained),
        }));
    }
    if journal.pending.is_some() {
        return Ok(json!({ "ok": false, "reason": "已有一次安装在结算前等着，不能连着装第二次" }));
    }
    verify_staged_file(paths, &retained.sha256, &retained.file)?;
    let prior_state = journal.state.clone();
    let mut journal = journal;
    // 回滚**不动** `staged`，也不给它编一个 `signed=true`：这一份的来源是「本机上一次成功结算
    // 时留下的包」，它的可信度来自内容哈希（上面刚复核过），不来自清单。写成 signed=true
    // 会把「本机留存」冒充成「清单验签通过」，那是给账本造假。
    journal.pending = Some(Pending {
        version: retained.version.clone(),
        file: retained.file.clone(),
        action: ACTION_ROLLBACK.to_string(),
        previous_version: app_version(),
        launched_at_ms: now_ms() as i64,
    });
    journal.state = STATE_INSTALL_PENDING.to_string();
    journal.updated_at_ms = now_ms() as i64;
    write_journal(paths, &journal)?;
    let pid = match spawn_plan(&plan) {
        Ok(pid) => pid,
        Err(e) => {
            journal.pending = None;
            journal.state = prior_state;
            journal.last_error = e.clone();
            journal.note = "回滚器没能启动，状态已退回原样".to_string();
            write_journal(paths, &journal)?;
            record(paths, ACTION_ROLLBACK, false, &err_code(&e), &e);
            return Err(e);
        }
    };
    record(paths, ACTION_ROLLBACK, true, "", &format!("回滚包 {} 已启动 pid={pid}", retained.file));
    Ok(json!({
        "ok": true,
        "launched": true,
        "pid": pid,
        "fromVersion": app_version(),
        "toVersion": retained.version,
        "plan": plan,
        "rollback": retained_row(&retained),
        "journalState": STATE_INSTALL_PENDING,
        "note": "回滚器已启动；本机不会自动退出，退出后下次启动会结算。回滚成功后不再有「更早一档」的回滚材料",
    }))
}

fn retained_row(r: &Retained) -> Value {
    json!({
        "version": r.version,
        "file": r.file,
        "sha256": r.sha256,
        "sizeBytes": r.size_bytes,
        "kind": r.kind,
        "keptAtMs": r.kept_at_ms,
    })
}

/// exec 车道里 `update_*` 的方法表（`contracts_exec.ts` 有同名清单，两边由契约测试核对）。
pub const METHODS: &[&str] = &[
    "update_describe",
    "update_channel_get",
    "update_channel_set",
    "update_trust_list",
    "update_trust_add",
    "update_trust_remove",
    "update_check",
    "update_download",
    "update_status",
    "update_install_plan",
    "update_install",
    "update_rollback_plan",
    "update_rollback",
    "update_import_local",
    "update_ledger",
];

fn request_field(payload: Option<&Value>, key: &str) -> String {
    payload
        .and_then(|p| p.get(key))
        .and_then(|v| v.as_str())
        .unwrap_or_default()
        .trim()
        .to_string()
}

/// exec 车道的分派入口：`update_*` 之外返回 `None`，交回车道继续按未实现报账。
pub async fn dispatch(method: &str, payload: Option<&Value>) -> Option<Result<Value, String>> {
    dispatch_in(&Paths::from_data_root(), method, payload).await
}

/// 分派的实现面：**数据根由调用方给**。
///
/// 生产入口只有上面那句（从环境变量/家目录解析）。测试走这一句、传自己的临时目录，
/// 因为 `TONGTIANWORK_HOME` 是全进程共享的：多个用例并行改它会互相踩，
/// 踩到的那次看起来像「更新回路把别人的数据根写了」。
pub async fn dispatch_in(
    paths: &Paths,
    method: &str,
    payload: Option<&Value>,
) -> Option<Result<Value, String>> {
    if !method.starts_with(METHOD_PREFIX) {
        return None;
    }
    let result: Result<Value, String> = match method {
        "update_describe" => Ok(describe_view(paths)),
        "update_channel_get" => {
            let (channel, note) = read_channel(paths);
            Ok(json!({
                "ok": true,
                "channel": channel_view(&channel, &note),
                "loadNote": note,
                "note": "未配置渠道时 enabled=false，界面按「没有更新渠道」显示，不放检查更新的按钮",
            }))
        }
        "update_channel_set" => set_channel(paths, payload),
        "update_trust_list" => {
            let (list, note) = anchors(paths);
            Ok(json!({
                "ok": true,
                "anchors": list.iter().map(anchor_view).collect::<Vec<_>>(),
                "embeddedCount": EMBEDDED_ANCHORS.len(),
                "loadNote": note,
                "note": "内嵌锚点删不掉；本机添加的可以逐条删除",
            }))
        }
        "update_trust_add" => add_anchor(
            paths,
            &request_field(payload, "publisherId"),
            &request_field(payload, "publicKey"),
        ),
        "update_trust_remove" => remove_anchor(paths, &request_field(payload, "publisherId")),
        "update_check" => check_once(paths).await,
        "update_download" => {
            let version = request_field(payload, "version");
            if version.is_empty() {
                Ok(json!({
                    "ok": false,
                    "reason": "要下载就得给出版本号；不默认下「最新的那条」，避免界面拼一个没核对过的版本就装上",
                }))
            } else {
                stage_version(paths, &version).await
            }
        }
        "update_status" => status_view(paths),
        "update_install_plan" => install_plan_of(
            paths,
            &request_field(payload, "file"),
            &request_field(payload, "version"),
            ACTION_UPGRADE,
        ),
        "update_install" => launch_staged(
            paths,
            &request_field(payload, "file"),
            &request_field(payload, "version"),
            ACTION_UPGRADE,
            &request_field(payload, "confirm"),
        ),
        "update_rollback_plan" => rollback_view(paths, ""),
        "update_rollback" => rollback_view(paths, &request_field(payload, "confirm")),
        "update_import_local" => import_local(
            paths,
            &request_field(payload, "path"),
            &request_field(payload, "version"),
            &request_field(payload, "confirm"),
        ),
        "update_ledger" => {
            let limit = payload
                .and_then(|p| p.get("limit"))
                .and_then(|v| v.as_u64())
                .unwrap_or(50) as usize;
            let (rows, bad, total) = read_ledger(paths, limit);
            Ok(json!({
                "ok": true,
                "rows": rows,
                "count": rows.len(),
                "totalLines": total,
                "truncated": total > rows.len(),
                "limit": limit.clamp(1, MAX_LEDGER_LINES),
                "badLines": bad,
                "file": paths.ledger_file().display().to_string(),
                "note": "本车道的动作留痕在数据根的文件里，今天还没并进统一审计（见台账 D-12 未做项）",
            }))
        }
        other => Err(format!(
            "{}:{LANE}::{other}",
            crate::ipc::NOT_IMPLEMENTED,
            LANE = "exec"
        )),
    };
    // 失败也要留痕：一个不记账的失败面等于让运维只能靠复现来猜
    if let Err(e) = &result {
        record(paths, method, false, &err_code(e), e);
    }
    Some(result)
}

/// 启动时结算一次（`lib.rs` 只调这一句，其它都不必知道）。
pub fn settle_at_startup() {
    let paths = Paths::from_data_root();
    match settle(&paths) {
        Ok(true) => record(&paths, "settle", true, "", "启动结算：本机版本已按上次动作核对"),
        Ok(false) => {}
        Err(e) => record(&paths, "settle", false, &err_code(&e), &e),
    }
}

fn bad_channel_name(detail: String) -> String {
    format!(
        "UPDATE_INVALID_CHANNEL: 渠道名只能是字母、数字、点、下划线与连字符，且不能以点或连字符开头（{detail}）"
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use ring::signature::KeyPair as _;
    use std::sync::Mutex;

    /// 同一份临时数据根：每个用例拿自己的子目录，避免互相踩。
    static COUNTER: Mutex<u64> = Mutex::new(0);

    fn temp_root(tag: &str) -> PathBuf {
        let mut guard = COUNTER.lock().unwrap_or_else(|e| e.into_inner());
        *guard += 1;
        let dir = std::env::temp_dir().join(format!(
            "tt-app-update-{tag}-{}-{guard}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn drop_root(dir: &Path) {
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn test_paths_shape_is_pinned_and_root_comes_from_the_single_source() {
        let p = Paths::from_data_root();
        assert_eq!(p.root.file_name().unwrap(), DIRNAME);
        assert_eq!(p.staging_dir(), p.root.join(STAGING_DIRNAME));
        assert_eq!(p.journal_file(), p.root.join("journal.json"));
        assert_eq!(p.ledger_file(), p.root.join("ledger.jsonl"));
        assert_eq!(p.trust_file(), p.root.join("trusted_publishers.json"));
        assert_eq!(p.channel_file(), p.root.join("channel.json"));
        // 数据根只经一处取：本模块不许自己读环境变量
        let source = include_str!("mod.rs");
        assert!(
            source.contains("crate::security::file_perm::data_root()"),
            "更新目录必须复用同一处数据根"
        );
        assert!(
            !source.split("fn from_data_root").next().unwrap_or("").contains("TONGTIANWORK_HOME"),
            "本模块不许自己读数据根环境变量"
        );
    }

    #[test]
    fn test_channel_url_rules() {
        assert!(normalize_channel_url("https://update.example.invalid/latest.json").is_ok());
        let (url, local) = normalize_channel_url("http://127.0.0.1:7171/latest.json").unwrap();
        assert!(local);
        assert!(!url.contains('#'), "fragment 必须丢掉");
        let err = normalize_channel_url("http://update.example.invalid/latest.json").unwrap_err();
        assert!(err.starts_with("UPDATE_INVALID_URL:"), "{err}");
        assert!(err.contains("HTTPS"), "{err}");
        assert!(normalize_channel_url("https://u:p@example.invalid/a.json").unwrap_err().contains("用户名或密码"));
        assert!(normalize_channel_url("ftp://example.invalid/a").unwrap_err().contains("http / https"));
        assert!(normalize_channel_url("not a url").unwrap_err().starts_with("UPDATE_INVALID_URL:"));
    }

    #[test]
    fn test_public_ip_table() {
        let v4 = |s: &str| IpAddr::V4(s.parse::<Ipv4Addr>().unwrap());
        assert!(ip_is_public(v4("8.8.8.8")));
        for private in ["127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.1.1", "172.16.0.1", "100.64.0.1", "0.0.0.0", "224.0.0.1", "240.0.0.1"] {
            assert!(!ip_is_public(v4(private)), "{private} 不该被当公网");
        }
        assert!(!ip_is_public(IpAddr::V6(Ipv6Addr::LOCALHOST)));
        assert!(!ip_is_public(IpAddr::V6(Ipv6Addr::new(0xfe80, 0, 0, 0, 0, 0, 0, 1))), "链路本地不是公网");
        assert!(ip_is_public(IpAddr::V6(Ipv6Addr::new(0x2606, 0x2800, 0x220, 1, 0x248, 0xa000, 0, 0x1))));
    }

    #[test]
    fn test_staged_name_roundtrip_and_rejects_strangers() {
        let sha = sha256_hex(b"installer");
        let name = staged_file_name("0.2.0", "windows", "x64", &sha, manifest::KIND_NSIS);
        assert_eq!(name, format!("renwoxing-0.2.0-windows-x64-{}.exe", &sha[..12]));
        let parsed = parse_staged_name(&name).unwrap();
        assert_eq!(parsed.version, "0.2.0");
        assert_eq!(parsed.hash12, sha[..12]);
        assert_eq!(parsed.extension, "exe");
        assert_eq!(parsed.file_name(), name, "解析出来的必须能原样拼回文件名");
        assert_eq!(kind_of_extension(&parsed.extension).as_deref(), Some(manifest::KIND_NSIS));
        // 预发布版本号里带 `-`：按段切五分的写法会把它认成外来文件，
        // 于是「这一份是哪个版本」在目录里看不出来，回滚按名字也找不到它
        let pre = staged_file_name("1.2.3-beta.1", "windows", "x64", &sha, manifest::KIND_MSI);
        let parsed = parse_staged_name(&pre).unwrap();
        assert_eq!(parsed.version, "1.2.3-beta.1");
        assert_eq!(parsed.hash12, sha[..12]);
        assert_eq!(kind_of_extension(&parsed.extension).as_deref(), Some(manifest::KIND_MSI));
        // 非规范写法（大写、带 v、前导零）不进暂存区：同一个版本只允许一个名字
        for not_canonical in [
            "renwoxing-V0.2.0-windows-x64-abcdef123456.exe",
            "renwoxing-0.2.0-windows-x64-ABCDEF123456.exe",
        ] {
            assert!(parse_staged_name(not_canonical).is_none(), "{not_canonical} 不是规范名");
        }
        // 外来文件名不许被认成自己的包（也就不会被 prune 误删）
        for stranger in [
            "other-0.2.0-windows-x64-abcdef123456.exe",
            "renwoxing-0.2.0-windows-x64-abcdef12345.zip",
            "renwoxing-0.2.0-windows-x64-abcdef1.exe",
            "renwoxing-0.2.0-windows-abcdef123456.exe",
            "renwoxing-0.2.0-solaris-x64-abcdef123456.exe",
            "renwoxing-0.2.0-windows-x64-abcdef123456.exe-",
            "notes.txt",
        ] {
            assert!(parse_staged_name(stranger).is_none(), "{stranger} 不该被认出来");
        }
    }

    #[test]
    fn test_journal_roundtrip_and_corrupt_is_kept_as_evidence() {
        let root = temp_root("journal");
        let paths = Paths::for_root(root.clone());
        let mut journal = Journal::fresh();
        journal.state = STATE_STAGED.to_string();
        journal.staged = Some(Staged {
            version: "0.2.0".into(),
            file: "renwoxing-0.2.0-windows-x64-aaaaaaaaaaaa.exe".into(),
            sha256: "a".repeat(64),
            size_bytes: 4096,
            kind: "nsis".into(),
            platform: "windows".into(),
            arch: "x64".into(),
            publisher_id: "official".into(),
            signed: true,
            origin: ORIGIN_MANIFEST.into(),
            staged_at_ms: 1,
        });
        write_journal(&paths, &journal).unwrap();
        let (read, note) = read_journal(&paths);
        assert_eq!(read, journal);
        assert!(note.is_empty(), "{note}");

        std::fs::write(paths.journal_file(), "{不是 JSON").unwrap();
        let (fresh, note) = read_journal(&paths);
        assert_eq!(fresh.state, STATE_EMPTY);
        assert!(note.contains("改名留证"), "{note}");
        assert!(!paths.journal_file().exists(), "坏账本必须被挪走，不能继续按原路径读");
        assert!(std::fs::read_dir(paths.root.clone())
            .unwrap()
            .flatten()
            .any(|e| e.file_name().to_string_lossy().contains("corrupt-")));

        // schema 不符同样留证重来，而不是把旧字段猜着读
        std::fs::write(
            paths.journal_file(),
            json!({"schema": "tt-app-update/9", "state": "staged"}).to_string(),
        )
        .unwrap();
        let (fresh, note) = read_journal(&paths);
        assert_eq!(fresh.state, STATE_EMPTY);
        assert!(note.contains("格式版本"), "{note}");
        drop_root(&root);
    }

    #[test]
    fn test_settle_adopts_the_previous_version_package_as_rollback_material() {
        let root = temp_root("settle");
        let paths = Paths::for_root(root.clone());
        paths.ensure().unwrap();
        // 暂存区里有两份包：上一版（0.0.9）与这一版（0.9.9）
        // 大小必须过 `MIN_ARTIFACT_BYTES` 那道地板：回滚材料按内容复核时，太小的文件
        // 会被当成「不像安装包」直接跳过，那样测的就不是判定而是夹具。
        let mut old_bytes = b"the installer of the previous version".to_vec();
        old_bytes.resize(4096, 0x5A);
        let old_sha = sha256_hex(&old_bytes);
        let old_file = staged_file_name("0.0.9", host_platform(), host_arch(), &old_sha, manifest::KIND_NSIS);
        std::fs::write(paths.staged_file(&old_file), &old_bytes).unwrap();
        let mut new_bytes = b"the installer we just applied".to_vec();
        new_bytes.resize(4096, 0x37);
        let new_sha = sha256_hex(&new_bytes);
        let new_file = staged_file_name("0.9.9", host_platform(), host_arch(), &new_sha, manifest::KIND_NSIS);
        std::fs::write(paths.staged_file(&new_file), &new_bytes).unwrap();
        // 一份认不出名字的包：既不参与判定，也不该被当成回滚材料
        std::fs::write(paths.staged_file("renwoxing-9.9.9-windows-x64-bbbbbbbbbbbb.exe"), b"x".repeat(10)).unwrap();

        let mut journal = Journal::fresh();
        journal.state = STATE_INSTALL_PENDING.to_string();
        journal.staged = Some(staged_of("0.9.9", &new_file, &new_sha, &new_bytes));
        journal.pending = Some(Pending {
            version: "0.9.9".into(),
            file: new_file.clone(),
            action: ACTION_UPGRADE.to_string(),
            previous_version: "0.0.9".into(),
            launched_at_ms: 1,
        });
        write_journal(&paths, &journal).unwrap();

        // 升级成功：本机版本现在是 0.9.9。回滚材料必须是**上一个版本**那一份，不是刚装的这份。
        assert!(settle_with(&paths, "0.9.9").unwrap(), "有 pending 时结算必须改动状态");
        let (journal, _) = read_journal(&paths);
        assert_eq!(journal.state, STATE_INSTALLED);
        assert!(journal.pending.is_none());
        assert!(journal.staged.is_none(), "装完了就不再有「待装」这一份");
        assert!(journal.note.contains("0.9.9"), "{}", journal.note);
        let rollback = journal.rollback.clone().expect("上一版本的包要升级为回滚材料");
        assert_eq!(rollback.version, "0.0.9");
        assert_eq!(rollback.file, old_file);
        assert_eq!(rollback.sha256, old_sha);
        assert!(!settle_with(&paths, "0.9.9").unwrap(), "没有 pending 时结算不重复改状态");
        assert_eq!(read_journal(&paths).0.state, STATE_INSTALLED);

        // 版本没变过来：如实写失败，且不清掉暂存的包与回滚材料（用户还要靠它们重试/回退）
        let mut journal = journal;
        journal.state = STATE_INSTALL_PENDING.to_string();
        journal.pending = Some(Pending {
            version: "9.9.9".into(),
            file: "renwoxing-9.9.9-windows-x64-bbbbbbbbbbbb.exe".into(),
            action: ACTION_UPGRADE.into(),
            previous_version: "0.9.9".into(),
            launched_at_ms: 2,
        });
        write_journal(&paths, &journal).unwrap();
        settle_with(&paths, "0.9.9").unwrap();
        let (journal, _) = read_journal(&paths);
        assert_eq!(journal.state, STATE_INSTALL_FAILED);
        assert!(journal.last_error.contains("仍是"), "{}", journal.last_error);
        assert_eq!(journal.rollback.as_ref().unwrap().version, "0.0.9", "失败不能顺手改回滚材料");

        // 装了别的东西（版本变了但不是目标）：也不能含糊
        let mut journal = journal;
        journal.pending = Some(Pending {
            version: "8.8.8".into(),
            file: "renwoxing-9.9.9-windows-x64-bbbbbbbbbbbb.exe".into(),
            action: ACTION_UPGRADE.into(),
            previous_version: "0.0.0".into(),
            launched_at_ms: 3,
        });
        write_journal(&paths, &journal).unwrap();
        settle_with(&paths, "7.7.7").unwrap();
        let (journal, _) = read_journal(&paths);
        assert_eq!(journal.state, STATE_INSTALL_FAILED);
        assert!(journal.last_error.contains("不一致"), "{}", journal.last_error);

        // 回滚成功：状态是 rolled-back，且**不再留**回滚材料（那份已经是当前版本，留着只会诱导空回滚）
        let mut journal = journal;
        journal.state = STATE_INSTALL_PENDING.to_string();
        journal.rollback = Some(Retained {
            version: "0.0.9".into(),
            file: old_file.clone(),
            sha256: old_sha.clone(),
            size_bytes: old_bytes.len() as u64,
            kind: manifest::KIND_NSIS.into(),
            kept_at_ms: 1,
        });
        journal.pending = Some(Pending {
            version: "0.0.9".into(),
            file: old_file.clone(),
            action: ACTION_ROLLBACK.into(),
            previous_version: "7.7.7".into(),
            launched_at_ms: 4,
        });
        write_journal(&paths, &journal).unwrap();
        settle_with(&paths, "0.0.9").unwrap();
        let (journal, _) = read_journal(&paths);
        assert_eq!(journal.state, STATE_ROLLED_BACK);
        assert!(journal.rollback.is_none(), "回滚成功后不留「更早一档」的材料");
        drop_root(&root);
    }

    fn staged_of(version: &str, file: &str, sha: &str, bytes: &[u8]) -> Staged {
        Staged {
            version: version.to_string(),
            file: file.to_string(),
            sha256: sha.to_string(),
            size_bytes: bytes.len() as u64,
            kind: manifest::KIND_NSIS.to_string(),
            platform: host_platform().to_string(),
            arch: host_arch().to_string(),
            publisher_id: "tongtian-official".to_string(),
            signed: true,
            origin: ORIGIN_MANIFEST.to_string(),
            staged_at_ms: 1,
        }
    }

    #[test]
    fn test_rollback_material_is_matched_by_content_not_name() {
        let root = temp_root("rollback");
        let paths = Paths::for_root(root.clone());
        paths.ensure().unwrap();
        let installer = b"one".repeat(2048);
        let sha = sha256_hex(&installer);
        let file = staged_file_name("0.0.9", host_platform(), host_arch(), &sha, manifest::KIND_NSIS);
        std::fs::write(paths.staged_file(&file), &installer).unwrap();
        let mut journal = Journal::fresh();
        journal.state = STATE_INSTALL_PENDING.into();
        journal.pending = Some(Pending {
            version: "0.9.9".into(),
            file: "renwoxing-0.9.9-windows-x64-cccccccccccc.exe".into(),
            action: ACTION_UPGRADE.into(),
            previous_version: "0.0.9".into(),
            launched_at_ms: 1,
        });
        write_journal(&paths, &journal).unwrap();
        settle_with(&paths, "0.9.9").unwrap();
        assert_eq!(read_journal(&paths).0.rollback.as_ref().unwrap().file, file);

        // 文件名对、内容被换掉 ⇒ 完整哈希与名字里那 12 位对不上 ⇒ 回滚材料作废（不是照旧采信）
        std::fs::write(paths.staged_file(&file), b"tampered".repeat(256)).unwrap();
        let mut journal = Journal::fresh();
        journal.state = STATE_INSTALL_PENDING.into();
        journal.pending = Some(Pending {
            version: "1.0.0".into(),
            file: "renwoxing-1.0.0-windows-x64-dddddddddddd.exe".into(),
            action: ACTION_UPGRADE.into(),
            previous_version: "0.0.9".into(),
            launched_at_ms: 2,
        });
        write_journal(&paths, &journal).unwrap();
        settle_with(&paths, "1.0.0").unwrap();
        let (journal, _) = read_journal(&paths);
        assert!(journal.rollback.is_none(), "内容对不上的包不能当回滚材料");
        assert_eq!(journal.state, STATE_INSTALLED, "结算本身仍然如实完成");
        drop_root(&root);
    }

    #[test]
    fn test_prune_keeps_referenced_and_recent_drops_the_rest() {
        let root = temp_root("prune");
        let paths = Paths::for_root(root.clone());
        paths.ensure().unwrap();
        for index in 0..6 {
            let sha = sha256_hex(format!("installer-{index}").as_bytes());
            let name = staged_file_name(
                &format!("0.2.{index}"),
                host_platform(),
                host_arch(),
                &sha,
                manifest::KIND_NSIS,
            );
            std::fs::write(paths.staged_file(&name), format!("bytes-{index}")).unwrap();
        }
        std::fs::write(paths.staging_dir().join("外来文件.txt"), b"keep me").unwrap();
        let removed = prune_staging(&paths);
        assert_eq!(removed.len(), 3, "6 个包留下上限内的 3 个：{removed:?}");
        let names: Vec<String> = list_staged(&paths)
            .into_iter()
            .map(|v| v["file"].as_str().unwrap_or_default().to_string())
            .collect();
        assert!(names.iter().any(|n| n == "外来文件.txt"), "外来文件不许被清理顺手删掉：{names:?}");
        // 账本引用的那一份即使在最旧也要保住
        let mut journal = Journal::fresh();
        journal.rollback = Some(Retained {
            version: "0.2.0".into(),
            file: names[0].clone(),
            sha256: sha256_hex(b"bytes-0"),
            size_bytes: 7,
            kind: "nsis".into(),
            kept_at_ms: 1,
        });
        write_journal(&paths, &journal).unwrap();
        let removed = prune_staging(&paths);
        assert!(!removed.contains(&names[0]), "账本引用的包被删了：{removed:?}");
        drop_root(&root);
    }

    #[test]
    fn test_ledger_appends_and_trims_to_the_latest_lines() {
        let root = temp_root("ledger");
        let paths = Paths::for_root(root.clone());
        for index in 0..(MAX_LEDGER_LINES + 20) {
            record(&paths, "update_check", index % 2 == 0, "UPDATE_FETCH_FAILED", &format!("第 {index} 次"));
        }
        let (rows, bad, total) = read_ledger(&paths, 500);
        assert_eq!(rows.len(), MAX_LEDGER_LINES, "留痕必须截到上限");
        assert_eq!(bad, 0);
        // 写入侧本身就按上限截断（见 `record`），所以「文件里的条数」最多就是上限本身
        assert_eq!(total, MAX_LEDGER_LINES, "写满之后总数应等于上限，而不是实际写过 520 条");
        // 回话里是最近的那批、且倒序（最新的先给用户看）
        assert!(rows[0]["detail"].as_str().unwrap().contains("第 519 次"), "{}", rows[0]);
        assert!(rows[MAX_LEDGER_LINES - 1]["detail"].as_str().unwrap().contains("第 20 次"));
        assert_eq!(read_ledger(&paths, 3).0.len(), 3, "limit 生效");
        std::fs::write(paths.ledger_file(), "不是 JSON\n").unwrap();
        assert_eq!(read_ledger(&paths, 10).1, 1, "坏行要如实计数而不是静默丢掉");
        drop_root(&root);
    }

    #[test]
    fn test_trust_anchors_are_additive_and_conflicts_refused() {
        let root = temp_root("trust");
        let paths = Paths::for_root(root.clone());
        assert_eq!(EMBEDDED_ANCHORS.len(), 0, "今天内嵌锚点必须为空：仓库还没有发布私钥");
        let (_pair, public) = test_keypair();
        let added = add_anchor(&paths, "TongTian-Official", &public).unwrap();
        assert_eq!(added["added"], json!(true));
        assert_eq!(added["anchor"]["publisherId"], json!("tongtian-official"), "标识归一小写");
        assert_eq!(added["anchor"]["removable"], json!(true));
        let same = add_anchor(&paths, "tongtian-official", &public).unwrap();
        assert_eq!(same["added"], json!(false), "重复添加同一把钥匙不写第二份");

        // 同标识换钥匙：拒，而不是「两把都信」
        let (_other_pair, other_public) = test_keypair();
        let err = add_anchor(&paths, "tongtian-official", &other_public).unwrap_err();
        assert!(err.starts_with("UPDATE_PUBLISHER_KEY_CONFLICT:"), "{err}");
        assert!(anchors(&paths).0.len() == 1);

        let removed = remove_anchor(&paths, "tongtian-official").unwrap();
        assert_eq!(removed["removed"], json!(true));
        assert!(anchors(&paths).0.is_empty());
        let again = remove_anchor(&paths, "tongtian-official").unwrap();
        assert_eq!(again["ok"], json!(false), "删不存在的不算成功");
        let bad_key = Anchor::from_raw("p", &"A".repeat(86), "local").unwrap_err();
        assert!(bad_key.starts_with("UPDATE_INVALID_PUBLISHER_KEY:"), "{bad_key}");
        drop_root(&root);
    }

    fn test_keypair() -> (ring::signature::Ed25519KeyPair, String) {
        let rng = ring::rand::SystemRandom::new();
        let pkcs8 = ring::signature::Ed25519KeyPair::generate_pkcs8(&rng).unwrap();
        let pair = ring::signature::Ed25519KeyPair::from_pkcs8(pkcs8.as_ref()).unwrap();
        let public = base64::Engine::encode(
            &base64::engine::general_purpose::STANDARD_NO_PAD,
            pair.public_key().as_ref(),
        );
        (pair, public)
    }

    #[test]
    fn test_install_plan_is_exact_and_host_bound() {
        let path = Path::new("C:/temp/renwoxing-0.2.0-windows-x64-aaaaaaaaaaaa.exe");
        // 非 Windows 上任何安装命令都给不出：这是本机事实，不许编一条「大概能跑」的
        if !cfg!(windows) {
            for kind in [manifest::KIND_NSIS, manifest::KIND_MSI] {
                let err = install_plan(kind, path, ACTION_UPGRADE).unwrap_err();
                assert!(err.starts_with("UPDATE_UNSUPPORTED_ARTIFACT:"), "{err}");
            }
            assert!(install_plan("zip", path, ACTION_UPGRADE).is_err());
            return;
        }
        let plan = install_plan(manifest::KIND_NSIS, path, ACTION_UPGRADE).unwrap();
        assert_eq!(plan.program, path.to_string_lossy().to_string());
        assert_eq!(plan.args, vec!["/S".to_string()]);
        assert_eq!(plan.action, ACTION_UPGRADE);
        assert!(plan.quit_note.contains("退出"), "{}", plan.quit_note);
        assert!(plan.elevation_note.contains("currentUser"), "{}", plan.elevation_note);
        let msi = install_plan(manifest::KIND_MSI, Path::new("C:/temp/a.msi"), ACTION_ROLLBACK).unwrap();
        assert_eq!(msi.program, "msiexec");
        assert_eq!(
            msi.args,
            vec!["/i", "C:/temp/a.msi", "/quiet", "/norestart"]
                .into_iter()
                .map(str::to_string)
                .collect::<Vec<_>>()
        );
        assert_eq!(msi.action, ACTION_ROLLBACK);
        // 没有已知静默命令的类型必须拒，而不是退回「直接双击运行」
        let err = install_plan("zip", path, ACTION_UPGRADE).unwrap_err();
        assert!(err.starts_with("UPDATE_UNSUPPORTED_ARTIFACT:"), "{err}");
    }

    #[test]
    fn test_launch_requires_the_confirm_word_and_never_spawns_without_it() {
        let root = temp_root("confirm");
        let paths = Paths::for_root(root.clone());
        paths.ensure().unwrap();
        // 没有暂存包时：连计划也给不出，且明确指路（业务拒绝不是服务故障）
        let out = install_plan_of(&paths, "x.exe", "0.2.0", ACTION_UPGRADE).unwrap();
        assert_eq!(out["ok"], json!(false));
        assert!(out["reason"].as_str().unwrap().contains("update_download"), "{}", out["reason"]);
        assert_eq!(out["staged"], json!(null));
        let out = launch_staged(&paths, "x.exe", "0.2.0", ACTION_UPGRADE, "").unwrap();
        assert_eq!(out["ok"], json!(false));
        assert!(out["reason"].as_str().unwrap().contains(CONFIRM_WORD));

        let bytes = b"installer".to_vec();
        let sha = sha256_hex(&bytes);
        let file = staged_file_name("0.9.9", host_platform(), host_arch(), &sha, manifest::KIND_NSIS);
        std::fs::write(paths.staged_file(&file), &bytes).unwrap();
        let mut journal = Journal::fresh();
        journal.state = STATE_STAGED.to_string();
        journal.staged = Some(Staged {
            version: "0.9.9".into(),
            file: file.clone(),
            sha256: sha.clone(),
            size_bytes: bytes.len() as u64,
            kind: manifest::KIND_NSIS.into(),
            platform: host_platform().into(),
            arch: host_arch().into(),
            publisher_id: "official".into(),
            signed: true,
            origin: ORIGIN_MANIFEST.into(),
            staged_at_ms: 1,
        });
        write_journal(&paths, &journal).unwrap();

        // 带错版本号/文件名：不按下别的包
        let out = launch_staged(&paths, "elsewhere.exe", "0.1.0", ACTION_UPGRADE, CONFIRM_WORD).unwrap();
        assert_eq!(out["ok"], json!(false));
        assert!(out["reason"].as_str().unwrap().contains("本机暂存的是"));

        // 哈希已经变了：装之前再比一次这一步必须拦住
        std::fs::write(paths.staged_file(&file), b"tampered!").unwrap();
        let err = launch_staged(&paths, &file, "0.9.9", ACTION_UPGRADE, CONFIRM_WORD).unwrap_err();
        assert!(err.starts_with("UPDATE_HASH_MISMATCH:"), "{err}");
        let (journal, _) = read_journal(&paths);
        assert_eq!(journal.state, STATE_STAGED, "哈希不符时状态不推进");
        assert!(journal.pending.is_none(), "被拦住的安装绝不能留下 pending");

        // 回滚不许从这条路径走：它认的是账本里留存的那一份，不是「当前暂存的那一份」
        let err = launch_staged(&paths, &file, "0.9.9", ACTION_ROLLBACK, CONFIRM_WORD).unwrap_err();
        assert!(err.starts_with("UPDATE_INVALID_REQUEST:"), "{err}");
        assert!(read_journal(&paths).0.pending.is_none(), "走错路径也不许落 pending");
        drop_root(&root);
    }

    #[test]
    fn test_import_local_needs_its_own_word_and_records_unsigned() {
        let root = temp_root("import");
        let paths = Paths::for_root(root.clone());
        let outside = temp_root("import-src");
        std::fs::create_dir_all(&outside).unwrap();
        let mut bytes = b"nsis installer bytes".to_vec();
        bytes.resize(4096, 0x11);
        let source = outside.join("hand-passed.exe");
        std::fs::write(&source, &bytes).unwrap();

        let out = import_local(&paths, source.to_str().unwrap(), "0.3.0", "").unwrap();
        assert_eq!(out["ok"], json!(false));
        assert!(out["reason"].as_str().unwrap().contains(IMPORT_CONFIRM_WORD));

        let out = import_local(&paths, source.to_str().unwrap(), "0.3.0", IMPORT_CONFIRM_WORD).unwrap();
        assert_eq!(out["ok"], json!(true), "{out}");
        let staged = &out["staged"];
        assert_eq!(staged["signed"], json!(false), "手工包必须如实标成未签名");
        assert_eq!(staged["origin"], json!(ORIGIN_LOCAL));
        assert_eq!(staged["sha256"], json!(sha256_hex(&bytes)));
        assert_eq!(staged["kind"], json!(manifest::KIND_NSIS));
        let (journal, _) = read_journal(&paths);
        assert_eq!(journal.state, STATE_STAGED);

        // 后缀不对、路径不存在、版本号没说：都如实拒，且不写账本
        let zip = outside.join("a.zip");
        std::fs::write(&zip, vec![7u8; 4096]).unwrap();
        assert!(import_local(&paths, zip.to_str().unwrap(), "0.4.0", IMPORT_CONFIRM_WORD).unwrap()["ok"] == json!(false));
        assert!(import_local(&paths, (outside.join("没有这个文件.exe")).to_str().unwrap(), "0.4.0", IMPORT_CONFIRM_WORD).is_err());
        assert!(import_local(&paths, source.to_str().unwrap(), "不是版本号", IMPORT_CONFIRM_WORD).is_err());
        assert!(import_local(&paths, "", "0.4.0", IMPORT_CONFIRM_WORD).unwrap()["reason"]
            .as_str()
            .unwrap()
            .contains("路径"));

        // 太小不像安装包
        let tiny = outside.join("tiny.exe");
        std::fs::write(&tiny, b"x").unwrap();
        assert!(import_local(&paths, tiny.to_str().unwrap(), "0.5.0", IMPORT_CONFIRM_WORD).unwrap()["ok"] == json!(false));
        drop_root(&root);
        drop_root(&outside);
    }

    #[test]
    fn test_status_view_describes_the_empty_honest_state() {
        let root = temp_root("status");
        let paths = Paths::for_root(root.clone());
        let out = status_view(&paths).unwrap();
        assert_eq!(out["state"], json!(STATE_EMPTY));
        assert_eq!(out["rollbackAvailable"], json!(false));
        assert_eq!(out["staged"], json!(null));
        assert_eq!(out["channel"]["configured"], json!(false));
        assert!(out["trustedAnchors"].as_array().unwrap().is_empty());
        assert_eq!(out["appVersion"], json!(app_version()));
        // 路径按传进来的数据根如实回报（生产入口才拼 `update/`，见 `Paths::from_data_root`）
        assert_eq!(out["paths"]["dir"].as_str().unwrap(), paths.root.display().to_string());
        assert!(out["paths"]["journal"].as_str().unwrap().ends_with(JOURNAL_FILENAME));
        assert!(out["paths"]["staging"].as_str().unwrap().ends_with(STAGING_DIRNAME));

        let described = describe_view(&paths);
        assert_eq!(described["schema"], json!(SCHEMA));
        assert_eq!(described["signingDomain"], json!("tt-app-update-v1"));
        assert_eq!(described["embeddedAnchorCount"], json!(0));
        assert_eq!(described["methods"].as_array().unwrap().len(), METHODS.len());
        assert!(described["sampleManifest"]["schema"].as_str() == Some(SCHEMA));
        assert!(described["engineVersionNote"].as_str().unwrap().contains("ENGINE_VERSION"));
        drop_root(&root);
    }

    #[test]
    fn test_rollback_refuses_without_retained_package() {
        let root = temp_root("norollback");
        let paths = Paths::for_root(root.clone());
        let out = rollback_view(&paths, "").unwrap();
        assert_eq!(out["ok"], json!(false));
        assert!(out["reason"].as_str().unwrap().contains("update_import_local"), "{}", out["reason"]);
        assert_eq!(out["rollbackAvailable"], json!(false));
        drop_root(&root);
    }

    fn runtime() -> tokio::runtime::Runtime {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap()
    }

    #[test]
    fn test_dispatch_routes_only_our_prefix_and_reports_strangers_honestly() {
        let rt = runtime();
        let root = temp_root("dispatch");
        let paths = Paths::for_root(root.clone());
        // 非 update_ 的方法必须返回 None，让车道继续按 NOT_IMPLEMENTED 报账
        assert!(rt
            .block_on(dispatch_in(&paths, "chat:submit", None))
            .is_none());
        assert!(rt
            .block_on(dispatch_in(&paths, "workspace:read", None))
            .is_none());
        let err = rt
            .block_on(dispatch_in(&paths, "update_not_a_real_method", None))
            .unwrap()
            .unwrap_err();
        assert!(err.starts_with("IPC_LANE_NOT_IMPLEMENTED:exec::update_"), "{err}");
        let described = rt
            .block_on(dispatch_in(&paths, "update_describe", None))
            .unwrap()
            .unwrap();
        assert_eq!(described["schema"], json!(SCHEMA));
        assert_eq!(described["lane"], json!("exec"));
        assert_eq!(described["methodPrefix"], json!(METHOD_PREFIX));
        // 留痕口径：服务故障（Err）进账，业务拒绝（ok=false）不进账
        // ——否则「没配渠道」这种正常状态会被记成一串失败，运维看账本看不出哪里真坏了
        let out = rt
            .block_on(dispatch_in(&paths, "update_download", Some(&json!({"version": "0.2.0"}))))
            .unwrap()
            .unwrap();
        assert_eq!(out["ok"], json!(false), "没渠道时 download 只能拒绝：{out}");
        let text = std::fs::read_to_string(paths.ledger_file()).unwrap_or_default();
        assert!(
            text.contains("IPC_LANE_NOT_IMPLEMENTED"),
            "车道里的失败必须落进留痕：{text}"
        );
        assert!(
            !text.contains("\"action\":\"update_download\""),
            "业务拒绝不该被记成失败：{text}"
        );
        assert!(!text.contains("UPDATE_INVALID_URL"), "{text}");
        drop_root(&root);
    }

    #[test]
    fn test_channel_set_persists_and_can_be_cleared() {
        let rt = runtime();
        let root = temp_root("channel");
        let paths = Paths::for_root(root.clone());
        let out = rt
            .block_on(dispatch_in(
                &paths,
                "update_channel_set",
                Some(&json!({"url": "https://update.example.invalid/latest.json", "channel": "STABLE", "enabled": true})),
            ))
            .unwrap()
            .unwrap();
        assert_eq!(out["ok"], json!(true));
        assert_eq!(out["channel"]["channel"], json!("stable"), "渠道名归一小写");
        assert_eq!(out["channel"]["enabled"], json!(true));
        // 这一步没有发过网络请求，所以回话里不许出现「可达/已连通」这类由这次调用推断的说法
        let text = out.to_string();
        for claim in ["reachable", "reachableNow", "已连通", "可达"] {
            assert!(!text.contains(claim), "保存渠道不该声称连通性：{text}");
        }
        assert!(out["note"].as_str().unwrap().contains("没有发起网络请求"));
        let got = rt
            .block_on(dispatch_in(&paths, "update_channel_get", None))
            .unwrap()
            .unwrap();
        assert_eq!(got["channel"]["url"], json!("https://update.example.invalid/latest.json"));
        assert_eq!(got["channel"]["configured"], json!(true));

        // 启用但不给地址：拒，不写一个「启用但空地址」的状态
        let err = rt
            .block_on(dispatch_in(
                &paths,
                "update_channel_set",
                Some(&json!({"enabled": true})),
            ))
            .unwrap()
            .unwrap_err();
        assert!(err.starts_with("UPDATE_INVALID_URL:"), "{err}");
        // 非法渠道名
        let err = rt
            .block_on(dispatch_in(
                &paths,
                "update_channel_set",
                Some(&json!({"url": "https://a.invalid/x.json", "channel": "-bad"})),
            ))
            .unwrap()
            .unwrap_err();
        assert!(err.starts_with("UPDATE_INVALID_CHANNEL:"), "{err}");
        assert!(err.contains("渠道名"), "{err}");
        // 清除
        let out = rt
            .block_on(dispatch_in(
                &paths,
                "update_channel_set",
                Some(&json!({"url": ""})),
            ))
            .unwrap()
            .unwrap();
        assert_eq!(out["channel"]["configured"], json!(false));
        assert!(out["note"].as_str().unwrap().contains("没有更新渠道"));
        assert!(read_channel(&paths).0.url.is_empty());
        drop_root(&root);
    }

    #[test]
    fn test_trust_methods_and_ledger_are_reachable_through_the_lane() {
        let rt = runtime();
        let root = temp_root("lane");
        let paths = Paths::for_root(root.clone());
        let (_pair, public) = test_keypair();
        let added = rt
            .block_on(dispatch_in(
                &paths,
                "update_trust_add",
                Some(&json!({"publisherId": "official", "publicKey": public.clone()})),
            ))
            .unwrap()
            .unwrap();
        assert_eq!(added["added"], json!(true));
        let listed = rt
            .block_on(dispatch_in(&paths, "update_trust_list", None))
            .unwrap()
            .unwrap();
        assert_eq!(listed["anchors"].as_array().unwrap().len(), 1);
        assert_eq!(listed["anchors"][0]["publicKey"], json!(public_normalize(&public)));
        let removed = rt
            .block_on(dispatch_in(
                &paths,
                "update_trust_remove",
                Some(&json!({"publisherId": "official"})),
            ))
            .unwrap()
            .unwrap();
        assert_eq!(removed["removed"], json!(true));
        let ledger = rt
            .block_on(dispatch_in(&paths, "update_ledger", Some(&json!({"limit": 10}))))
            .unwrap()
            .unwrap();
        assert_eq!(ledger["ok"], json!(true));
        assert!(ledger["note"].as_str().unwrap().contains("还没并进统一审计"));
        let status = rt
            .block_on(dispatch_in(&paths, "update_status", None))
            .unwrap()
            .unwrap();
        assert_eq!(status["state"], json!(STATE_EMPTY));
        assert!(status["settledNow"] == json!(false));
        drop_root(&root);
    }

    fn public_normalize(raw: &str) -> String {
        Anchor::from_raw("p", raw, "local").unwrap().public_key
    }

    #[test]
    fn test_download_and_check_refuse_honestly_without_a_channel() {
        let rt = runtime();
        let root = temp_root("nochanel");
        let paths = Paths::for_root(root.clone());
        // 「没有渠道」是业务状态，回 ok=false + 理由，不是 Err：
        // 报 Err 会让界面把它读成服务故障，也会让账本多出一次并不存在的失败
        let out = rt
            .block_on(dispatch_in(&paths, "update_check", None))
            .unwrap()
            .unwrap();
        assert_eq!(out["ok"], json!(false), "{out}");
        assert!(out["reason"].as_str().unwrap().contains("还没有配置更新渠道"));
        assert_eq!(out["channel"]["configured"], json!(false));
        let out = rt
            .block_on(dispatch_in(&paths, "update_download", Some(&json!({"version": "0.2.0"}))))
            .unwrap()
            .unwrap();
        assert_eq!(out["ok"], json!(false), "没渠道时 download 只能拒绝：{out}");
        let out = rt
            .block_on(dispatch_in(&paths, "update_download", None))
            .unwrap()
            .unwrap();
        assert_eq!(out["ok"], json!(false), "不给版本号时不许默认下最新版");
        // 没渠道时也不许留失败痕迹（它是状态，不是失败）
        let text = std::fs::read_to_string(paths.ledger_file()).unwrap_or_default();
        assert!(!text.contains("UPDATE_INVALID_URL"), "{text}");
        drop_root(&root);
    }

    #[test]
    fn test_methods_table_matches_the_prefix_rule_and_has_no_duplicates() {
        assert!(!METHODS.is_empty());
        for m in METHODS {
            assert!(m.starts_with(METHOD_PREFIX), "{m} 不属于本模块前缀");
        }
        let unique: BTreeSet<&str> = METHODS.iter().copied().collect();
        assert_eq!(unique.len(), METHODS.len(), "方法表里有重复");
        // 表里每一条都得在 dispatch 的 match 里出现（漏一条就是「契约说有、跑了报未实现」）
        let source = include_str!("mod.rs");
        for m in METHODS {
            assert!(source.contains(&format!("\"{m}\"")), "{m} 在方法表里但没被 dispatch 接住");
        }
    }

    #[test]
    fn test_state_table_is_closed_and_only_known_states_reach_the_journal() {
        assert_eq!(STATES.len(), 6, "状态是闭合的六个：见本模块头部的流程");
        let unique: BTreeSet<&str> = STATES.iter().copied().collect();
        assert_eq!(unique.len(), STATES.len(), "状态表里有重复");
        for state in [
            STATE_EMPTY,
            STATE_STAGED,
            STATE_INSTALL_PENDING,
            STATE_INSTALLED,
            STATE_ROLLED_BACK,
            STATE_INSTALL_FAILED,
        ] {
            assert!(STATES.contains(&state), "{state} 不在状态表里");
        }
        // 代码只许通过表里的常量写状态；出现字符串字面量就说明有人发明了第六个以外的状态。
        // 只扫生产段：测试段里故意写了个表外的值，用来验证读侧的 stateKnown=false。
        let source = include_str!("mod.rs");
        let production = source.split("#[cfg(test)]").next().unwrap_or(source);
        assert!(
            !production.contains("journal.state = \""),
            "状态只能通过 STATES 里的常量写进账本"
        );
        assert!(!production.contains("STATE_UNKNOWN"), "不许发明没在表里的状态");

        // 账本被外部改成表外的值时，界面读到的必须是 stateKnown=false，而不是被当成某个已知状态
        let root = temp_root("state");
        let paths = Paths::for_root(root.clone());
        let mut journal = Journal::fresh();
        journal.state = "half-installed".into();
        write_journal(&paths, &journal).unwrap();
        let out = status_view(&paths).unwrap();
        assert_eq!(out["state"], json!("half-installed"), "原样回出来，不改写别人的账");
        assert_eq!(out["stateKnown"], json!(false));
        assert!(out["stateNotes"].as_str().unwrap().contains("闭合状态表"), "{:?}", out["stateNotes"]);
        drop_root(&root);
    }

    #[test]
    fn test_artifact_view_and_helpers_agree_with_manifest() {
        // 这条是跨文件的一致性钉子：本模块对外回的形状必须由 manifest 的形状推出来
        let mut art = decode_base64("").unwrap_or_default();
        art.push(1);
        let value = json!({"kind": manifest::KIND_NSIS});
        assert_eq!(value["kind"].as_str(), Some(manifest::KIND_NSIS));
        assert_eq!(kind_extension(manifest::KIND_NSIS), Some("exe"));
        assert!(artifact_view(&test_artifact())["url"].is_string());
        assert_eq!(err_code("UPDATE_HASH_MISMATCH: 详情"), "UPDATE_HASH_MISMATCH");
        assert_eq!(err_code("没有冒号的句子"), "");
    }

    fn test_artifact() -> Artifact {
        Artifact {
            platform: host_platform().to_string(),
            arch: host_arch().to_string(),
            kind: manifest::KIND_NSIS.to_string(),
            url: "https://update.example.invalid/a.exe".to_string(),
            sha256: "a".repeat(64),
            size_bytes: 4096,
            publisher_id: "official".to_string(),
            signature: vec![0u8; 64],
            signature_b64: "A".repeat(86),
        }
    }

    /// 在本机回环起一个最小 HTTP 服务：按请求行里的路径回对应的字节，其它路径回 404。
    ///
    /// 没有它，「取清单 → 验签 → 下载 → 比哈希 → 落暂存」这条真路径就只能以纯函数形式被测，
    /// 而 D-12 的验收问的是「新版本可检测、下载、校验、安装」。这一族代码今天能测到的
    /// 上限就是「有源时该怎么走」；真发布端点仍是未做项。
    /// 地址先由 `std` 的 socket 占好，调用方才能把带端口的产物 URL 写进清单。
    fn bind_loopback() -> (String, std::net::TcpListener) {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("回环端口应当可绑");
        let addr = listener.local_addr().expect("已绑定的 socket 有本地地址");
        listener.set_nonblocking(true).expect("非阻塞开关应当可设");
        (format!("http://{addr}"), listener)
    }

    fn spawn_accept_loop(
        listener: std::net::TcpListener,
        routes: Vec<(String, Vec<u8>)>,
    ) -> tokio::task::JoinHandle<()> {
        let listener = tokio::net::TcpListener::from_std(listener).expect("转成 tokio 监听器");
        tokio::spawn(async move {
            loop {
                let Ok((mut stream, _)) = listener.accept().await else {
                    break;
                };
                let routes = routes.clone();
                tokio::spawn(async move {
                    use tokio::io::{AsyncReadExt, AsyncWriteExt};
                    let mut head = [0u8; 4096];
                    let read = stream.read(&mut head).await.unwrap_or(0);
                    let line = String::from_utf8_lossy(&head[..read]).to_string();
                    let path = line.split_whitespace().nth(1).unwrap_or_default().to_string();
                    let (status, body) = match routes.iter().find(|(p, _)| *p == path) {
                        Some((_, body)) => ("200 OK", body.clone()),
                        None => ("404 Not Found", b"no such route".to_vec()),
                    };
                    let header = format!(
                        "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                        body.len()
                    );
                    let _ = stream.write_all(header.as_bytes()).await;
                    let _ = stream.write_all(&body).await;
                    let _ = stream.shutdown().await;
                });
            }
        })
    }

    /// 造一份「本机能装、签名合法」的清单：`sha` 与 `publisher` 由调用方决定，
    /// 因此既能造合法的那份，也能造「签名没错但哈希与真文件不符」和「发布者不认识」的那两份。
    fn build_signed_manifest(
        pair: &ring::signature::Ed25519KeyPair,
        base: &str,
        channel: &str,
        version: &str,
        sha: &str,
        artifact_bytes: usize,
        publisher: &str,
    ) -> String {
        let payload = manifest::signing_payload(
            channel,
            version,
            host_platform(),
            host_arch(),
            manifest::KIND_NSIS,
            publisher,
            sha,
        );
        let signature = base64::Engine::encode(
            &base64::engine::general_purpose::STANDARD_NO_PAD,
            pair.sign(&payload).as_ref(),
        );
        json!({
            "schema": SCHEMA,
            "channel": channel,
            "publishedAtMs": 1_730_000_000_000i64,
            "revokedVersions": [],
            "releases": [{
                "version": version,
                "notes": "回环流测试用的清单",
                "artifacts": [{
                    "platform": host_platform(),
                    "arch": host_arch(),
                    "kind": manifest::KIND_NSIS,
                    "url": format!("{base}/artifact.exe"),
                    "sha256": sha,
                    "sizeBytes": artifact_bytes,
                    "publisherId": publisher,
                    "signature": signature,
                }],
            }],
        })
        .to_string()
    }

    #[test]
    fn test_full_flow_checks_downloads_and_never_installs_unconfirmed() {
        let rt = runtime();
        let root = temp_root("flow");
        let paths = Paths::for_root(root.clone());
        let (pair, public) = test_keypair();
        add_anchor(&paths, "tongtian-official", &public).unwrap();
        let installer: Vec<u8> = vec![0x4du8; 4096];
        let sha = sha256_hex(&installer);
        let version = "0.9.9";

        rt.block_on(async {
            let (base, listener) = bind_loopback();
            let good = build_signed_manifest(&pair, &base, "stable", version, &sha, installer.len(), "tongtian-official");
            // 签名对着「假哈希」签：验签会过，只有下载后的哈希核对拦得住它
            let lie = sha256_hex(b"a completely different payload");
            let bad = build_signed_manifest(&pair, &base, "stable", "2.0.0", &lie, installer.len(), "tongtian-official");
            // 签名与内容全对，但发布者不在这台机器的信任列表里
            let (other_pair, _other_key) = test_keypair();
            let foreign = build_signed_manifest(&other_pair, &base, "stable", "3.0.0", &sha, installer.len(), "tongtian-official");
            let _server = spawn_accept_loop(
                listener,
                vec![
                    ("/latest.json".to_string(), good.clone().into_bytes()),
                    ("/bad.json".to_string(), bad.clone().into_bytes()),
                    ("/foreign.json".to_string(), foreign.clone().into_bytes()),
                    ("/artifact.exe".to_string(), installer.clone()),
                ],
            );

            // 1) 配渠道（只写本机文件，不发请求）
            let out = dispatch_in(
                &paths,
                "update_channel_set",
                Some(&json!({"url": format!("{base}/latest.json"), "channel": "stable", "enabled": true})),
            )
            .await
            .unwrap()
            .unwrap();
            assert_eq!(out["ok"], json!(true), "{out}");

            // 2) 看：验签在下载之前完成，且这条本机可装
            let checked = dispatch_in(&paths, "update_check", None)
                .await
                .unwrap()
                .unwrap();
            assert_eq!(checked["ok"], json!(true), "{checked}");
            assert_eq!(checked["rows"].as_array().unwrap().len(), 1);
            assert_eq!(checked["rows"][0]["version"], json!(version));
            assert_eq!(checked["rows"][0]["installable"], json!(true), "{checked}");
            assert_eq!(checked["rows"][0]["signature"]["ok"], json!(true));
            assert_eq!(checked["installable"]["version"], json!(version));
            assert!(checked["notices"].as_array().unwrap().is_empty(), "{:?}", checked["notices"]);

            // 3) 下：原子改名进暂存区，落盘内容与清单声明逐字一致
            let out = dispatch_in(&paths, "update_download", Some(&json!({"version": version})))
                .await
                .unwrap()
                .unwrap();
            assert_eq!(out["ok"], json!(true), "{out}");
            let file = out["staged"]["file"].as_str().unwrap().to_string();
            assert_eq!(
                file,
                staged_file_name(version, host_platform(), host_arch(), &sha, manifest::KIND_NSIS)
            );
            assert_eq!(out["staged"]["sha256"], json!(sha));
            assert_eq!(out["staged"]["sizeBytes"], json!(installer.len()));
            assert_eq!(std::fs::read(paths.staged_file(&file)).unwrap(), installer);
            let leftovers: Vec<String> = std::fs::read_dir(paths.staging_dir())
                .unwrap()
                .flatten()
                .map(|e| e.file_name().to_string_lossy().to_string())
                .filter(|n| n.starts_with('.'))
                .collect();
            assert!(leftovers.is_empty(), "下载残留了临时文件：{leftovers:?}");
            assert_eq!(read_journal(&paths).0.state, STATE_STAGED);

            // 4) 计划：只给命令，不执行
            let plan = dispatch_in(
                &paths,
                "update_install_plan",
                Some(&json!({"file": file.clone(), "version": version})),
            )
            .await
            .unwrap();
            if cfg!(windows) {
                let plan = plan.unwrap();
                assert_eq!(plan["ok"], json!(true), "{plan}");
                assert_eq!(plan["plan"]["args"], json!(["/S"]));
                assert_eq!(plan["confirmWord"], json!(CONFIRM_WORD));
            } else {
                assert!(plan.unwrap_err().starts_with("UPDATE_UNSUPPORTED_ARTIFACT:"));
            }

            // 5) 不给确认词：不启动安装器，也不落 pending
            let out = dispatch_in(
                &paths,
                "update_install",
                Some(&json!({"file": file.clone(), "version": version})),
            )
            .await
            .unwrap()
            .unwrap();
            assert_eq!(out["ok"], json!(false), "{out}");
            assert!(out["reason"].as_str().unwrap().contains(CONFIRM_WORD));
            let (journal, _) = read_journal(&paths);
            assert!(journal.pending.is_none(), "没给确认词不许落 pending");
            assert_eq!(journal.state, STATE_STAGED);

            // 6) 换渠道到「假哈希」那份：验签过了，下载这一步必须拦住且什么也不留
            dispatch_in(
                &paths,
                "update_channel_set",
                Some(&json!({"url": format!("{base}/bad.json"), "channel": "stable", "enabled": true})),
            )
            .await
            .unwrap()
            .unwrap();
            let checked = dispatch_in(&paths, "update_check", None).await.unwrap().unwrap();
            assert_eq!(checked["rows"][0]["signature"]["ok"], json!(true), "这份的签名本身是合法的");
            let err = dispatch_in(&paths, "update_download", Some(&json!({"version": "2.0.0"})))
                .await
                .unwrap()
                .unwrap_err();
            assert!(err.starts_with("UPDATE_HASH_MISMATCH:"), "{err}");
            let names: Vec<String> = std::fs::read_dir(paths.staging_dir())
                .unwrap()
                .flatten()
                .map(|e| e.file_name().to_string_lossy().to_string())
                .collect();
            assert!(names.iter().all(|n| !n.starts_with('.')), "哈希不符的下载留下了临时文件：{names:?}");
            assert!(
                names.iter().all(|n| !n.starts_with(&format!("{PRODUCT_TOKEN}-2.0.0-"))),
                "哈希不符的包进了暂存区：{names:?}"
            );
            assert_eq!(read_journal(&paths).0.state, STATE_STAGED, "被拦住的下载不动状态");

            // 7) 发布者不认识：验签在下载之前就拒，且理由要说清是「没信任」而不是「签错了」
            dispatch_in(
                &paths,
                "update_channel_set",
                Some(&json!({"url": format!("{base}/foreign.json"), "channel": "stable", "enabled": true})),
            )
            .await
            .unwrap()
            .unwrap();
            // 这份清单的发布者字段是 tongtian-official，但签名是另一把钥匙做的 ⇒ 签名不匹配
            let checked = dispatch_in(&paths, "update_check", None).await.unwrap().unwrap();
            assert_eq!(checked["rows"][0]["installable"], json!(false), "{checked}");
            assert_eq!(
                checked["rows"][0]["signature"]["code"],
                json!("UPDATE_SIGNATURE_INVALID"),
                "{checked}"
            );
            let out = dispatch_in(&paths, "update_download", Some(&json!({"version": "3.0.0"})))
                .await
                .unwrap()
                .unwrap();
            assert_eq!(out["ok"], json!(false), "验不过签的包不该走到下载：{out}");
            assert!(out["reason"].as_str().unwrap().contains("UPDATE_SIGNATURE_INVALID:"), "{out}");

            // 8) 本机一条锚点也没有时是第三种拒法：先加钥匙，别和「签错了」混成一句
            let parsed = parse_manifest(good.clone().as_bytes(), "stable").unwrap();
            let (release, art) = host_artifact_of(&parsed, version).expect("清单里有本机这一条");
            let no_anchor_root = temp_root("flow-noanchor");
            let no_anchor_paths = Paths::for_root(no_anchor_root.clone());
            assert!(anchors(&no_anchor_paths).0.is_empty());
            let err = verify_artifact(&parsed, &release, &art, &anchors(&no_anchor_paths).0).unwrap_err();
            assert!(err.starts_with("UPDATE_NO_TRUST_ANCHOR:"), "{err}");
            let err = verify_artifact(&parsed, &release, &art, &[]).unwrap_err();
            assert!(err.starts_with("UPDATE_NO_TRUST_ANCHOR:"), "{err}");
            // 加了另一把钥匙（同一个发布者标识不能有两把钥匙，这里换的是发布者不认识的那种）
            let (_p2, k2) = test_keypair();
            add_anchor(&no_anchor_paths, "someone-else", &k2).unwrap();
            let err = verify_artifact(&parsed, &release, &art, &anchors(&no_anchor_paths).0).unwrap_err();
            assert!(err.starts_with("UPDATE_UNTRUSTED_PUBLISHER:"), "{err}");
            drop_root(&no_anchor_root);
        });
        drop_root(&root);
    }
}
