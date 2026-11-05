//! 已安装清单、检查更新与撤销禁用（D-07 客户端段）
//!
//! 本机三份状态（都在 settings_kv）：
//! - `market.installed`：装过的条目（kind + itemId + 版本 + sha256 + 来源主机 + 时间）
//! - `market.revoked`：最近一次检查确认「已被撤销」的条目（执行面拦截依据）
//! - `market.lastCheckAtMs`：最近一次检查时间，页面据此显示「上次检查」
//!
//! 为什么撤销单独存一份而不是只写 `skill.disabled`：禁用开关是用户能拨的，
//! 撤销是外部权威的判断。已撤销的包即使把开关拨回「启用」，也不该重新进入执行面，
//! 所以运行时装配读 `market.revoked`，不读用户开关。
//!
//! 撤销只作用于**确实从市场装过的记录**（provenance = market）。本机手写/导入的同名
//! 技能与套件不参与比对：否则市场里某个 id 被撤销，会把用户自己的同名目录一起禁掉，
//! 那是误伤而不是保护。版本对不上时也不阻断——只能确认「被撤销的是另一个版本」。

use std::path::Path;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::bridge::event_store::now_ms;
use crate::security::audit;
use crate::skill;
use crate::store::{settings, Store};
use crate::suite;

use super::{RevokedDecl, KIND_SKILL, KIND_SUITE};

const REGISTRY_KEY: &str = "market.installed";
const REVOKED_KEY: &str = "market.revoked";
const LAST_CHECK_KEY: &str = "market.lastCheckAtMs";
/// 清单上限：与市场索引条目上限同量级，超出时按安装时间保留最近的
const MAX_RECORDS: usize = 500;

/// 市场安装来源标记：只有这一类记录参与撤销比对
pub const PROVENANCE_MARKET: &str = "market";
/// 本机扫盘补齐的记录（历史上装过但没落清单，或本地导入）
const PROVENANCE_SCANNED: &str = "scanned";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct InstalledRecord {
    pub kind: String,
    pub item_id: String,
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub version: Option<String>,
    #[serde(default)]
    pub sha256: Option<String>,
    #[serde(default)]
    pub source_host: Option<String>,
    #[serde(default)]
    pub installed_at_ms: u64,
    #[serde(default)]
    pub provenance: Option<String>,
}

/// 一条撤销声明在本机的落盘形态（保留原因与时间，供界面与审计复用）
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct RevokedMark {
    pub kind: String,
    pub item_id: String,
    #[serde(default)]
    pub version: Option<String>,
    #[serde(default)]
    pub reason: Option<String>,
    #[serde(default)]
    pub marked_at_ms: u64,
}

fn item_key(kind: &str, id: &str) -> String {
    format!("{kind}\u{1}{id}")
}

/// 读已安装清单：损坏或非法 JSON 按「空清单」处理，不阻断市场页面
pub fn load_registry(store: &Store) -> Vec<InstalledRecord> {
    settings::get(store, REGISTRY_KEY)
        .ok()
        .flatten()
        .and_then(|raw| serde_json::from_str::<Vec<InstalledRecord>>(&raw).ok())
        .unwrap_or_default()
}

fn save_registry(store: &Store, list: &[InstalledRecord]) -> Result<(), String> {
    let mut trimmed: Vec<InstalledRecord> = list.to_vec();
    if trimmed.len() > MAX_RECORDS {
        trimmed.sort_by_key(|r| std::cmp::Reverse(r.installed_at_ms));
        trimmed.truncate(MAX_RECORDS);
    }
    let encoded = serde_json::to_string(&trimmed)
        .map_err(|e| format!("MARKET_INVALID_INDEX: 已安装清单序列化失败（{e}）"))?;
    settings::set(store, REGISTRY_KEY, &encoded)
}

/// 读撤销标记清单（损坏时按「无撤销」处理，与禁用清单同容错口径）
pub fn load_revoked(store: &Store) -> Vec<RevokedMark> {
    settings::get(store, REVOKED_KEY)
        .ok()
        .flatten()
        .and_then(|raw| serde_json::from_str::<Vec<RevokedMark>>(&raw).ok())
        .unwrap_or_default()
}

fn save_revoked(store: &Store, list: &[RevokedMark]) -> Result<(), String> {
    let encoded = serde_json::to_string(list)
        .map_err(|e| format!("MARKET_INVALID_INDEX: 撤销清单序列化失败（{e}）"))?;
    settings::set(store, REVOKED_KEY, &encoded)
}

/// 执行面拦截依据：某一类里已被撤销、因此不许再进上下文的条目标识
pub fn blocked_ids(store: &Store, kind: &str) -> Vec<String> {
    load_revoked(store)
        .into_iter()
        .filter(|mark| mark.kind == kind)
        .map(|mark| mark.item_id)
        .collect()
}

/// 该条目是否已被撤销（启用开关与安装前的闸门用）
pub fn revoked_reason_for(store: &Store, kind: &str, item_id: &str) -> Option<String> {
    load_revoked(store)
        .into_iter()
        .find(|mark| mark.kind == kind && mark.item_id == item_id)
        .map(|mark| mark.reason.unwrap_or_else(|| "市场未给出原因".to_string()))
}

/// 安装成功后落清单：同一 kind+itemId 覆盖旧记录，先清掉它的撤销标记
///
/// 清标记是必要的：装的是索引当前提供的版本，说明这一份没被撤销；
/// 留着旧标记会让新装的版本一直不可执行。
pub fn note_install(
    store: &Store,
    kind: &str,
    item_id: &str,
    name: Option<String>,
    version: Option<String>,
    sha256: Option<String>,
    source_host: Option<String>,
) -> Result<(), String> {
    let mut list = load_registry(store);
    list.retain(|r| item_key(&r.kind, &r.item_id) != item_key(kind, item_id));
    list.push(InstalledRecord {
        kind: kind.to_string(),
        item_id: item_id.to_string(),
        name,
        version,
        sha256,
        source_host,
        installed_at_ms: now_ms(),
        provenance: Some(PROVENANCE_MARKET.to_string()),
    });
    save_registry(store, &list)?;
    let mut marks = load_revoked(store);
    let before = marks.len();
    marks.retain(|m| item_key(&m.kind, &m.item_id) != item_key(kind, item_id));
    if marks.len() != before {
        save_revoked(store, &marks)?;
    }
    Ok(())
}

/// 卸载后清掉清单与撤销标记（文件已经不在，标记留着只会让下一个同名条目被误伤）
pub fn forget(store: &Store, kind: &str, item_id: &str) -> Result<(), String> {
    let mut list = load_registry(store);
    let before = list.len();
    list.retain(|r| item_key(&r.kind, &r.item_id) != item_key(kind, item_id));
    if list.len() != before {
        save_registry(store, &list)?;
    }
    let mut marks = load_revoked(store);
    let before = marks.len();
    marks.retain(|m| item_key(&m.kind, &m.item_id) != item_key(kind, item_id));
    if marks.len() != before {
        save_revoked(store, &marks)?;
    }
    Ok(())
}

/// 把磁盘上真实存在、但清单里没有的条目补进来（标成 scanned，不带版本）
///
/// 补齐记录只用于展示「本机装了哪些」，不参与撤销比对：我们并不知道它来自哪里。
fn contains(list: &[InstalledRecord], kind: &str, id: &str) -> bool {
    list.iter().any(|r| r.kind == kind && r.item_id == id)
}

fn backfill(list: &mut Vec<InstalledRecord>, base: &Path) {
    for row in skill::list_in(base, &[]) {
        let Some(id) = row.get("id").and_then(Value::as_str) else {
            continue;
        };
        if !contains(list, KIND_SKILL, id) {
            list.push(InstalledRecord {
                kind: KIND_SKILL.to_string(),
                item_id: id.to_string(),
                name: row.get("name").and_then(Value::as_str).map(str::to_owned),
                version: None,
                sha256: None,
                source_host: None,
                installed_at_ms: row
                    .get("installedAtMs")
                    .and_then(Value::as_u64)
                    .unwrap_or_default(),
                provenance: Some(PROVENANCE_SCANNED.to_string()),
            });
        }
    }
    for row in suite::list_in(base, &[]) {
        let Some(id) = row.get("id").and_then(Value::as_str) else {
            continue;
        };
        if !contains(list, KIND_SUITE, id) {
            list.push(InstalledRecord {
                kind: KIND_SUITE.to_string(),
                item_id: id.to_string(),
                name: row.get("name").and_then(Value::as_str).map(str::to_owned),
                version: None,
                sha256: None,
                source_host: None,
                installed_at_ms: row
                    .get("installedAtMs")
                    .and_then(Value::as_u64)
                    .unwrap_or_default(),
                provenance: Some(PROVENANCE_SCANNED.to_string()),
            });
        }
    }
}

/// 撤销声明是否命中某条已安装记录
///
/// 命中条件：类型一致、标识一致（忽略大小写）、且声明未指定版本或版本与本机一致。
/// 本机版本未知时只有「全版本撤销」才命中——拿不确定的版本去阻断，误伤大于保护。
pub fn decl_matches(decl: &RevokedDecl, kind: &str, item_id: &str, local_version: Option<&str>) -> bool {
    if let Some(decl_kind) = decl.kind.as_deref() {
        let lowered = decl_kind.trim().to_lowercase();
        let norm = match lowered.as_str() {
            "skill" | "skills" => KIND_SKILL,
            "suite" | "suites" => KIND_SUITE,
            other => other,
        };
        if norm != kind {
            return false;
        }
    }
    if !decl.id.trim().eq_ignore_ascii_case(item_id) {
        return false;
    }
    match (decl.version.as_deref(), local_version) {
        (None, _) => true,
        (Some(_), None) => false,
        (Some(decl_version), Some(local)) => {
            decl_version.trim().eq_ignore_ascii_case(local.trim())
        }
    }
}

/// 点分数字版本比较的结论：本机相对索引是哪一档
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VersionGap {
    /// 索引版本更高 → 可以更新
    IndexNewer,
    /// 两边一致 → 已是最新
    Same,
    /// 本机版本更高（本地装了还没发布的版本，或索引回退）
    InstalledNewer,
    /// 任一方不是点分数字，或版本未知 → 只能如实说「比不了」
    Unknown,
}

/// 点分数字版本比较（`1.2.3`、补零对齐）；比不了时返回 `VersionGap::Unknown`
pub fn version_order(a: Option<&str>, b: Option<&str>) -> VersionGap {
    let parts = |raw: &str| -> Option<Vec<u64>> {
        let segs: Vec<&str> = raw
            .trim()
            .trim_start_matches('v')
            .split('.')
            .map(str::trim)
            .collect();
        if segs.is_empty() {
            return None;
        }
        segs.iter()
            .map(|s| s.parse::<u64>().ok())
            .collect::<Option<Vec<u64>>>()
    };
    let (Some(left), Some(right)) = (a.and_then(parts), b.and_then(parts)) else {
        return VersionGap::Unknown;
    };
    let mut left = left;
    let mut right = right;
    let len = left.len().max(right.len());
    left.resize(len, 0);
    right.resize(len, 0);
    for idx in 0..len {
        if left[idx] != right[idx] {
            return if right[idx] > left[idx] {
                VersionGap::IndexNewer
            } else {
                VersionGap::InstalledNewer
            };
        }
    }
    VersionGap::Same
}

/// 检查更新 + 应用撤销：拉索引（含 revokedUrl）→ 比对已安装清单 → 命中撤销即禁用
///
/// 返回的 `items[].status` 只有这几种：`upToDate` / `updateAvailable` / `ahead` /
/// `unknownVersion` / `delisted` / `revoked`；`blocked` 只在 revoked 时为 true。
/// 索引读不到时如实进 `errors`，其余条目照常给结论——不把「没连上」说成「没有更新」。
pub async fn check_updates(store: &Store, base: &Path) -> Result<Value, String> {
    let sources = super::read_sources(store);
    let mut registry = load_registry(store);
    backfill(&mut registry, base);

    let mut decls: Vec<RevokedDecl> = Vec::new();
    let mut latest: Vec<(String, Option<String>, Option<String>)> = Vec::new();
    let mut errors: Vec<String> = Vec::new();
    let mut configured = 0usize;

    if let Some(url) = sources.skill_index_url.as_deref() {
        configured += 1;
        match super::fetch_skill_index(url).await {
            Ok(index) => {
                for item in &index.items {
                    latest.push((
                        item_key(KIND_SKILL, &item.id),
                        item.version.clone(),
                        Some(item.name.clone()),
                    ));
                    if item.revoked {
                        decls.push(RevokedDecl {
                            kind: Some(KIND_SKILL.to_string()),
                            id: item.id.clone(),
                            version: item.version.clone(),
                            reason: item.revoked_reason.clone(),
                        });
                    }
                }
                decls.extend(index.revoked.iter().cloned());
                if !index.revoked_source_ok {
                    errors.push(format!(
                        "技能撤销清单没读到，本轮只用索引自带的声明：{}",
                        index.revoked_source_error.unwrap_or_default()
                    ));
                }
            }
            Err(e) => errors.push(format!("技能索引读取失败：{e}")),
        }
    }
    if let Some(url) = sources.suite_index_url.as_deref() {
        configured += 1;
        match super::fetch_suite_index(url).await {
            Ok(index) => {
                for item in &index.items {
                    latest.push((
                        item_key(KIND_SUITE, &item.id),
                        item.version.clone(),
                        Some(item.name.clone()),
                    ));
                    if item.revoked {
                        decls.push(RevokedDecl {
                            kind: Some(KIND_SUITE.to_string()),
                            id: item.id.clone(),
                            version: item.version.clone(),
                            reason: item.revoked_reason.clone(),
                        });
                    }
                }
                decls.extend(index.revoked.iter().cloned());
                if !index.revoked_source_ok {
                    errors.push(format!(
                        "套件撤销清单没读到，本轮只用索引自带的声明：{}",
                        index.revoked_source_error.unwrap_or_default()
                    ));
                }
            }
            Err(e) => errors.push(format!("套件索引读取失败：{e}")),
        }
    }

    let checked_at = now_ms();
    let mut marks: Vec<RevokedMark> = Vec::new();
    for rec in registry.iter().filter(|r| r.provenance.as_deref() == Some(PROVENANCE_MARKET)) {
        if let Some(decl) = decls
            .iter()
            .find(|decl| decl_matches(decl, &rec.kind, &rec.item_id, rec.version.as_deref()))
        {
            marks.push(RevokedMark {
                kind: rec.kind.clone(),
                item_id: rec.item_id.clone(),
                version: decl.version.clone().or_else(|| rec.version.clone()),
                reason: decl
                    .reason
                    .clone()
                    .or_else(|| Some("市场未给出原因".to_string())),
                marked_at_ms: checked_at,
            });
        }
    }
    save_revoked(store, &marks)?;

    // 已撤销 → 走既有的禁用清单，列表页与开关都显示「已禁用」
    let mut disabled_now: Vec<String> = Vec::new();
    for mark in &marks {
        let res = match mark.kind.as_str() {
            KIND_SKILL => skill::set_disabled(store, &mark.item_id, true),
            _ => suite::set_disabled(store, &mark.item_id, true),
        };
        match res {
            Ok(_) => disabled_now.push(format!("{}:{}", mark.kind, mark.item_id)),
            Err(e) => errors.push(format!("撤销后禁用失败（{}:{}）：{e}", mark.kind, mark.item_id)),
        }
    }

    let blocked: Vec<String> = marks
        .iter()
        .map(|m| item_key(&m.kind, &m.item_id))
        .collect();
    let items: Vec<Value> = registry
        .iter()
        .map(|rec| {
            let key = item_key(&rec.kind, &rec.item_id);
            let hit = latest.iter().find(|(k, _, _)| *k == key);
            let is_revoked = blocked.contains(&key);
            let latest_version = hit.and_then(|(_, v, _)| v.clone());
            let status = if is_revoked {
                "revoked"
            } else if hit.is_none() {
                "delisted"
            } else {
                match version_order(rec.version.as_deref(), latest_version.as_deref()) {
                    VersionGap::Same => "upToDate",
                    VersionGap::IndexNewer => "updateAvailable",
                    VersionGap::InstalledNewer => "ahead",
                    VersionGap::Unknown => "unknownVersion",
                }
            };
            json!({
                "kind": rec.kind,
                "id": rec.item_id,
                "name": rec.name,
                "installedVersion": rec.version,
                "latestVersion": latest_version,
                "sha256": rec.sha256,
                "sourceHost": rec.source_host,
                "installedAtMs": rec.installed_at_ms,
                "provenance": rec.provenance.clone().unwrap_or_else(|| PROVENANCE_MARKET.to_string()),
                "status": status,
                "updateAvailable": status == "updateAvailable",
                "blocked": is_revoked,
                "revokedReason": marks
                    .iter()
                    .find(|m| item_key(&m.kind, &m.item_id) == key)
                    .and_then(|m| m.reason.clone()),
            })
        })
        .collect();

    // 索引里的版本回填到清单展示字段（只补市场记录的空版本，不改安装事实）
    let mut dirty = false;
    for rec in registry.iter_mut() {
        if rec.provenance.as_deref() != Some(PROVENANCE_MARKET) {
            continue;
        }
        let key = item_key(&rec.kind, &rec.item_id);
        if rec.name.is_none() {
            if let Some(name) = latest.iter().find(|(k, _, _)| *k == key).and_then(|(_, _, n)| n.clone()) {
                rec.name = Some(name);
                dirty = true;
            }
        }
    }
    if dirty || backfill_changed(&registry) {
        save_registry(store, &registry)?;
    }
    settings::set(store, LAST_CHECK_KEY, &checked_at.to_string())?;

    for mark in &marks {
        audit::record_quiet(
            store,
            audit::ACTION_MARKET_REVOKE,
            audit::SEV_WARNING,
            &mark.item_id,
            &format!(
                "市场撤销了{}「{}」（版本 {}），本机已转禁用且不再进入执行面：{}",
                if mark.kind == KIND_SKILL { "技能" } else { "套件" },
                mark.item_id,
                mark.version.as_deref().unwrap_or("全部版本"),
                mark.reason.as_deref().unwrap_or("未给出原因")
            ),
        );
    }

    Ok(json!({
        "supported": true,
        "checkedAtMs": checked_at,
        "sourcesConfigured": configured,
        "errors": errors,
        "items": items,
        "summary": {
            "updateAvailable": items.iter().filter(|v| v.get("updateAvailable").and_then(Value::as_bool) == Some(true)).count(),
            "revoked": marks.len(),
            "delisted": items.iter().filter(|v| v.get("status").and_then(Value::as_str) == Some("delisted")).count(),
            "disabledApplied": disabled_now.len(),
        },
    }))
}

/// 补齐过的清单要落盘：否则下次扫描又补一遍，页面顺序会反复变
fn backfill_changed(registry: &[InstalledRecord]) -> bool {
    registry
        .iter()
        .any(|r| r.provenance.as_deref() == Some(PROVENANCE_SCANNED))
}

/// 已安装清单视图（不联网，供页面在检查前后都能显示本机状态）
pub fn installed_view(store: &Store, base: &Path) -> Value {
    let mut registry = load_registry(store);
    backfill(&mut registry, base);
    let blocked = load_revoked(store);
    let disabled_skills = skill::disabled_ids(store);
    let disabled_suites = suite::disabled_ids(store);
    let items: Vec<Value> = registry
        .iter()
        .map(|rec| {
            let mark = blocked.iter().find(|m| m.kind == rec.kind && m.item_id == rec.item_id);
            let is_disabled = if rec.kind == KIND_SKILL {
                disabled_skills.iter().any(|d| d == &rec.item_id)
            } else {
                disabled_suites.iter().any(|d| d == &rec.item_id)
            };
            json!({
                "kind": rec.kind,
                "id": rec.item_id,
                "name": rec.name,
                "installedVersion": rec.version,
                "sha256": rec.sha256,
                "sourceHost": rec.source_host,
                "installedAtMs": rec.installed_at_ms,
                "provenance": rec.provenance.clone().unwrap_or_else(|| PROVENANCE_MARKET.to_string()),
                "revoked": mark.is_some(),
                "revokedReason": mark.and_then(|m| m.reason.clone()),
                "disabled": is_disabled,
            })
        })
        .collect();
    json!({
        "supported": true,
        "items": items,
        "lastCheckAtMs": settings::get(store, LAST_CHECK_KEY)
            .ok()
            .flatten()
            .and_then(|raw| raw.parse::<u64>().ok()),
    })
}
