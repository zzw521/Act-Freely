//! 工作空间（workspace）目录事实来源
//!
//! command：
//! - workspace_list:   列出 `{data_home}/workspaces/*` 真实存在的工作空间，并合并 Agent 实例运行态
//! - workspace_create: 以增量方式创建目录（幂等：已存在直接返回，不覆盖任何既有内容）
//!
//! 设计约束：
//! - 目录即事实来源，不引入新表，因此无需迁移，历史数据不受影响；
//! - 标识严格校验（拒绝路径穿越与分隔符），避免用用户输入拼接出越界目录；
//! - 本模块不提供删除/挂起等破坏性能力：删除目录会毁掉工作区文件，与数据保护规范冲突。
//! - 纯函数接受 `base` 路径参数，便于用临时目录做单元测试，不触碰真实数据目录。

use std::path::Path;

use serde_json::{json, Value};
use tauri::State;

use crate::bridge::agent::{self, BridgeState};
use crate::bridge::python_env;

/// 缺省工作空间（引擎 work-dir 与文件白名单同源布局的默认分片）
pub const DEFAULT_WORKSPACE: &str = "default";

/// 工作空间标识最大长度
const MAX_ID_LEN: usize = 64;

/// 校验并归一工作空间标识：小写字母/数字/下划线/连字符，禁止路径分隔符与穿越
pub fn normalize_id(raw: &str) -> Result<String, String> {
    let id = raw.trim().to_lowercase();
    if id.is_empty() {
        return Err("WORKSPACE_INVALID_ID: 工作空间标识不能为空".into());
    }
    if id.len() > MAX_ID_LEN {
        return Err(format!(
            "WORKSPACE_INVALID_ID: 工作空间标识过长（{} 字符，上限 {}）",
            id.len(),
            MAX_ID_LEN
        ));
    }
    if id.contains('.') || id.contains('/') || id.contains('\\') {
        return Err(format!(
            "WORKSPACE_INVALID_ID: 工作空间标识「{id}」含路径分隔符或点号"
        ));
    }
    if !id
        .chars()
        .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-')
    {
        return Err(format!(
            "WORKSPACE_INVALID_ID: 工作空间标识「{id}」仅支持小写字母、数字、下划线与连字符"
        ));
    }
    Ok(id)
}

fn dir_mtime_ms(dir: &Path) -> u64 {
    dir.metadata()
        .and_then(|m| m.modified())
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

fn file_count(dir: &Path) -> usize {
    std::fs::read_dir(dir)
        .map(|rd| rd.filter_map(|e| e.ok()).count())
        .unwrap_or(0)
}

/// 目录视图（running 等运行态由调用方合并，纯函数只描述磁盘事实）
pub fn dir_info(base: &Path, id: &str) -> Value {
    let dir = base.join("workspaces").join(id);
    let exists = dir.is_dir();
    json!({
        "id": id,
        "name": id,
        "exists": exists,
        "createdAtMs": if exists { dir_mtime_ms(&dir) } else { 0 },
        "entryCount": if exists { file_count(&dir) } else { 0 },
        "rootPath": dir.to_string_lossy(),
        "running": false,
        "status": "stopped",
    })
}

/// 列出工作空间：`default` 恒在（目录未落地时 exists=false，不伪装成已存在），
/// 其余按标识字典序返回真实存在的目录。
pub fn list_in(base: &Path) -> Vec<Value> {
    let root = base.join("workspaces");
    let mut ids: Vec<String> = std::fs::read_dir(&root)
        .map(|rd| {
            rd.filter_map(|e| e.ok())
                .filter(|e| e.path().is_dir())
                .filter_map(|e| e.file_name().to_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default();
    ids.sort();
    ids.dedup();
    if !ids.iter().any(|id| id == DEFAULT_WORKSPACE) {
        ids.push(DEFAULT_WORKSPACE.to_string());
        ids.sort();
    }
    ids.into_iter().map(|id| dir_info(base, &id)).collect()
}

/// 创建工作空间目录（幂等：已存在时返回现状，不写入也不覆盖任何文件）
pub fn create_in(base: &Path, raw_id: &str) -> Result<Value, String> {
    let id = normalize_id(raw_id)?;
    let dir = base.join("workspaces").join(&id);
    if !dir.is_dir() {
        std::fs::create_dir_all(&dir).map_err(|e| {
            format!("WORKSPACE_CREATION_FAILED: 无法创建工作空间目录 {}：{e}", dir.display())
        })?;
    }
    Ok(dir_info(base, &id))
}

/// 合并 Agent 实例运行态（bridge_status 的 instances 数组按 workspaceId 对齐）
fn with_running(mut view: Value, instances: &[Value]) -> Value {
    let id = view.get("id").and_then(|v| v.as_str()).unwrap_or_default();
    if let Some(inst) = instances
        .iter()
        .find(|i| i.get("workspaceId").and_then(|v| v.as_str()) == Some(id))
    {
        let status = inst
            .get("status")
            .and_then(|v| v.as_str())
            .unwrap_or("stopped")
            .to_string();
        // InstanceStatus 以 lowercase 序列化（bridge/agent.rs 的 serde rename_all）
        let running = matches!(status.as_str(), "starting" | "ready" | "busy");
        if let Some(obj) = view.as_object_mut() {
            obj.insert("running".into(), json!(running));
            obj.insert("status".into(), json!(status));
            if let Some(port) = inst.get("port") {
                obj.insert("port".into(), port.clone());
            }
        }
    }
    view
}

/// 列出工作空间（真实目录 + 运行态）
#[tauri::command]
pub async fn workspace_list(state: State<'_, BridgeState>) -> Result<Value, String> {
    let status = agent::bridge_status(&state.inner()).await;
    let instances = status
        .get("instances")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    let views = list_in(&python_env::base_dir())
        .into_iter()
        .map(|v| with_running(v, &instances))
        .collect::<Vec<Value>>();
    Ok(Value::Array(views))
}

/// 创建工作空间目录（供「选择工作空间」下拉无合适项时新建）
#[tauri::command]
pub async fn workspace_create(workspace_id: String) -> Result<Value, String> {
    Ok(create_in(&python_env::base_dir(), &workspace_id)?)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn temp_base(tag: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!(
            "tt-workspace-{tag}-{}-{}",
            std::process::id(),
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&p).unwrap();
        p
    }

    #[test]
    fn normalizes_valid_id() {
        assert_eq!(normalize_id("  Team_A1 ").unwrap(), "team_a1");
    }

    #[test]
    fn rejects_empty_and_overlong_id() {
        assert!(normalize_id("").unwrap_err().starts_with("WORKSPACE_INVALID_ID"));
        assert!(normalize_id("   ")
            .unwrap_err()
            .starts_with("WORKSPACE_INVALID_ID"));
        let long = "a".repeat(MAX_ID_LEN + 1);
        assert!(normalize_id(&long).unwrap_err().starts_with("WORKSPACE_INVALID_ID"));
    }

    /// 路径穿越与分隔符必须拒绝（标识会拼进真实目录路径）
    #[test]
    fn rejects_path_traversal() {
        for bad in ["../evil", "a/b", "a\\b", "..", "a.b", "/abs"] {
            let err = normalize_id(bad).unwrap_err();
            assert!(
                err.starts_with("WORKSPACE_INVALID_ID"),
                "{bad} 应被拒，实得：{err}"
            );
        }
    }

    #[test]
    fn rejects_non_ascii_id() {
        for bad in ["我的空间", "a b", "A*B"] {
            assert!(normalize_id(bad)
                .unwrap_err()
                .starts_with("WORKSPACE_INVALID_ID"));
        }
    }

    #[test]
    fn empty_root_lists_default_as_not_existing() {
        let base = temp_base("empty");
        let rows = list_in(&base);
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["id"], DEFAULT_WORKSPACE);
        assert_eq!(rows[0]["exists"], false);
        assert_eq!(rows[0]["running"], false);
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn lists_existing_dirs_sorted_by_id() {
        let base = temp_base("list");
        create_in(&base, "team-b").unwrap();
        create_in(&base, "alpha").unwrap();
        let rows = list_in(&base);
        let ids: Vec<&str> = rows
            .iter()
            .map(|r| r["id"].as_str().unwrap())
            .collect();
        assert_eq!(ids, vec!["alpha", "default", "team-b"]);
        assert_eq!(rows[0]["exists"], true);
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn create_is_idempotent_and_preserves_files() {
        let base = temp_base("idempotent");
        create_in(&base, "report").unwrap();
        let file = base.join("workspaces").join("report").join("keep.md");
        std::fs::write(&file, "存量内容").unwrap();
        let again = create_in(&base, "report").unwrap();
        assert_eq!(again["exists"], true);
        assert!(file.exists(), "重复创建不得动目录内既有文件");
        assert_eq!(again["entryCount"].as_u64(), Some(1));
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn invalid_id_create_does_not_touch_disk() {
        let base = temp_base("invalid");
        assert!(create_in(&base, "../escape")
            .unwrap_err()
            .starts_with("WORKSPACE_INVALID_ID"));
        assert!(!base.join("workspaces").join("escape").exists());
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn merges_running_state_by_workspace() {
        let inst = vec![json!({ "workspaceId": "alpha", "port": 18001, "status": "ready" })];
        let merged = with_running(dir_info(Path::new("."), "alpha"), &inst);
        assert_eq!(merged["running"], true);
        assert_eq!(merged["status"], "ready");
        assert_eq!(merged["port"].as_u64(), Some(18001));

        let stopped = with_running(dir_info(Path::new("."), "beta"), &inst);
        assert_eq!(stopped["running"], false);
        assert_eq!(stopped["status"], "stopped");
    }
}
