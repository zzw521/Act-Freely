//! 安全工作环境 —「工作环境文件」的真实磁盘事实
//!
//! 目录约定：`{data_home}/workspaces/{workspace}/environment/**`
//! 引擎在启用安全工作环境且用户未自选工作目录时，把任务执行落在该目录下
//! （见 `engine/server.py` 的 `ENV_DIR_NAME`），因此它整体可丢弃。
//!
//! 清理边界（数据保护规范）：
//! - 只删 `environment/` 目录内的内容，保留目录本身（幂等，引擎可重建）；
//! - `environment/` 之外的一切（对话记录 sessions/、工作产物 files/、技能与意识目录）不触碰；
//! - 待删路径必须规范到 `{data_home}/workspaces/{id}/environment` 之下，越界即拒绝；
//! - 符号链接条目一律不删，避免顺着链接删到目录树之外。
//!
//! 纯函数接受 `base` 路径参数，单元测试用临时目录，不触碰真实数据目录。

use std::path::{Path, PathBuf};

use serde_json::{json, Value};

/// 工作环境子目录名（与 engine/server.py 的 ENV_DIR_NAME 同名同源）
pub const ENV_DIR_NAME: &str = "environment";

/// 单个工作空间的工作环境目录（不保证存在）
pub fn env_dir_in(base: &Path, workspace_id: &str) -> PathBuf {
    base.join("workspaces").join(workspace_id).join(ENV_DIR_NAME)
}

/// 已落地的工作空间标识（字典序，去重）
pub fn workspace_ids_in(base: &Path) -> Vec<String> {
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
    ids
}

/// 目录内文件的递归统计结果；`unreadable` 是读不动的条目数（权限占用等）
#[derive(Default, Clone, Copy)]
pub struct DirStat {
    pub bytes: u64,
    pub files: u64,
    pub unreadable: u64,
}

/// 递归统计目录大小。符号链接只算链接本身不跟随，避免环路；
/// 单个条目读不到只计入 `unreadable`，不中断整体统计。
pub fn stat_dir(dir: &Path) -> DirStat {
    fn walk(p: &Path, acc: &mut DirStat) {
        let entries = match std::fs::read_dir(p) {
            Ok(entries) => entries,
            Err(_) => {
                acc.unreadable += 1;
                return;
            }
        };
        for entry in entries.filter_map(|e| e.ok()) {
            let path = entry.path();
            let meta = match std::fs::symlink_metadata(&path) {
                Ok(meta) => meta,
                Err(_) => {
                    acc.unreadable += 1;
                    continue;
                }
            };
            if meta.is_dir() {
                walk(&path, acc);
            } else {
                acc.bytes += meta.len();
                acc.files += 1;
            }
        }
    }
    let mut acc = DirStat::default();
    if dir.is_dir() {
        walk(dir, &mut acc);
    }
    acc
}

/// 校验待清路径确实落在 `{base}/workspaces/{id}/environment` 之下。
/// 用 canonicalize 后的前缀比对，符号链接或 `..` 变体一律拒绝。
fn guard_env_path(base: &Path, target: &Path) -> Result<PathBuf, String> {
    let base_real = base.canonicalize().map_err(|e| format!("SANDBOX_ENV_BASE_MISSING: {e}"))?;
    let target_real = target
        .canonicalize()
        .map_err(|e| format!("SANDBOX_ENV_PATH_INVALID: {e}"))?;
    let marker = PathBuf::from("workspaces").join(ENV_DIR_NAME);
    let suffix = target_real.strip_prefix(&base_real).map_err(|_| {
        format!(
            "SANDBOX_ENV_PATH_OUT_OF_SCOPE: 拒绝清理越界路径 {}",
            target_real.display()
        )
    })?;
    // suffix 形如 workspaces/{id}/environment
    let parts: Vec<_> = suffix.components().collect();
    if parts.len() != 3 || parts[0].as_os_str() != "workspaces" || parts[2].as_os_str() != ENV_DIR_NAME {
        return Err(format!(
            "SANDBOX_ENV_PATH_OUT_OF_SCOPE: 仅允许清理 {} 下的工作空间，收到 {}",
            marker.display(),
            suffix.display()
        ));
    }
    Ok(target_real)
}

/// 用量视图（读磁盘，不改磁盘）
pub fn usage_in(base: &Path) -> Value {
    let mut total = DirStat::default();
    let mut workspaces: Vec<Value> = Vec::new();
    for id in workspace_ids_in(base) {
        let dir = env_dir_in(base, &id);
        if !dir.is_dir() {
            continue;
        }
        let stat = stat_dir(&dir);
        total.bytes += stat.bytes;
        total.files += stat.files;
        total.unreadable += stat.unreadable;
        workspaces.push(json!({
            "workspaceId": id,
            "path": dir.to_string_lossy(),
            "bytes": stat.bytes,
            "files": stat.files,
        }));
    }
    json!({
        "supported": true,
        "bytes": total.bytes,
        "files": total.files,
        "unreadable": total.unreadable,
        "workspaceCount": workspaces.len(),
        "workspaces": workspaces,
        "rootPath": base.join("workspaces").to_string_lossy(),
        "envDirName": ENV_DIR_NAME,
    })
}

/// 清理所有工作空间的环境文件，返回真实释放量。
/// 只删 `environment/` 内的内容，目录本身保留。
pub fn clear_in(base: &Path) -> Result<Value, String> {
    let mut freed_bytes = 0u64;
    let mut freed_files = 0u64;
    let mut cleared: Vec<Value> = Vec::new();
    let mut failed: Vec<Value> = Vec::new();

    for id in workspace_ids_in(base) {
        let dir = env_dir_in(base, &id);
        if !dir.is_dir() {
            continue;
        }
        let target = match guard_env_path(base, &dir) {
            Ok(p) => p,
            Err(e) => {
                failed.push(json!({ "workspaceId": id, "error": e }));
                continue;
            }
        };
        let before = stat_dir(&target);
        let entries = match std::fs::read_dir(&target) {
            Ok(entries) => entries,
            Err(e) => {
                failed.push(json!({
                    "workspaceId": id,
                    "error": format!("SANDBOX_ENV_CLEAR_FAILED: 无法读取 {}：{e}", target.display()),
                }));
                continue;
            }
        };
        for entry in entries.filter_map(|e| e.ok()) {
            let path = entry.path();
            let rm = if path.is_dir() && !path.is_symlink() {
                std::fs::remove_dir_all(&path)
            } else {
                std::fs::remove_file(&path)
            };
            if let Err(e) = rm {
                failed.push(json!({
                    "workspaceId": id,
                    "path": path.to_string_lossy(),
                    "error": format!("SANDBOX_ENV_CLEAR_FAILED: {e}"),
                }));
            }
        }
        // 以清理前后的实测差值作为释放量，删除失败的条目自然不计入
        let after = stat_dir(&target);
        let net_bytes = before.bytes.saturating_sub(after.bytes);
        let net_files = before.files.saturating_sub(after.files);
        if net_bytes > 0 || net_files > 0 {
            freed_bytes += net_bytes;
            freed_files += net_files;
            cleared.push(json!({
                "workspaceId": id,
                "freedBytes": net_bytes,
                "freedFiles": net_files,
            }));
        }
    }

    Ok(json!({
        "freedBytes": freed_bytes,
        "freedFiles": freed_files,
        "cleared": cleared,
        "failed": failed,
    }))
}

/// 清理失败条目对应的错误码，供 command 层判定是否整体失败
pub fn first_failure(result: &Value) -> Option<String> {
    result
        .get("failed")
        .and_then(|v| v.as_array())
        .and_then(|arr| arr.first())
        .and_then(|item| item.get("error"))
        .and_then(|v| v.as_str())
        .map(str::to_owned)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_base(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "tt-env-{tag}-{}-{}",
            std::process::id(),
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn write(path: &Path, content: &str) {
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, content).unwrap();
    }

    #[test]
    fn temp_base_is_reusable_across_platforms() {
        let base = temp_base("sanity");
        assert!(base.is_dir());
        assert!(workspace_ids_in(&base).is_empty());
        std::fs::remove_dir_all(&base).unwrap();
    }

    #[test]
    fn env_dir_path_layout() {
        let base = PathBuf::from("/tmp/home");
        assert_eq!(
            env_dir_in(&base, "default"),
            PathBuf::from("/tmp/home").join("workspaces").join("default").join(ENV_DIR_NAME)
        );
    }

    #[test]
    fn stat_dir_counts_nested_files_and_skips_missing() {
        let base = temp_base("stat");
        let env = env_dir_in(&base, "default");
        write(&env.join("a.txt"), "hello");
        write(&env.join("sub/b.bin"), "1234567890");
        let stat = stat_dir(&env);
        assert_eq!(stat.files, 2);
        assert_eq!(stat.bytes, 5 + 10);
        assert_eq!(stat_dir(&env.join("nope")).files, 0);
        std::fs::remove_dir_all(&base).unwrap();
    }

    #[test]
    fn usage_zero_when_no_env_dirs_landed() {
        let base = temp_base("usage-empty");
        std::fs::create_dir_all(base.join("workspaces").join("default")).unwrap();
        let v = usage_in(&base);
        assert_eq!(v["bytes"], 0);
        assert_eq!(v["files"], 0);
        assert_eq!(v["workspaceCount"], 0);
        assert!(v["workspaces"].as_array().unwrap().is_empty());
        std::fs::remove_dir_all(&base).unwrap();
    }

    #[test]
    fn usage_aggregates_each_workspace_env_dir() {
        let base = temp_base("usage-multi");
        write(&env_dir_in(&base, "alpha").join("x.txt"), "aaa");
        write(&env_dir_in(&base, "beta").join("y/z.txt"), "bb");
        // 非 environment 目录不计入
        write(&base.join("workspaces").join("beta").join("files").join("keep.txt"), "cccc");
        let v = usage_in(&base);
        assert_eq!(v["bytes"], 5);
        assert_eq!(v["files"], 2);
        assert_eq!(v["workspaceCount"], 2);
        let ids: Vec<&str> = v["workspaces"]
            .as_array()
            .unwrap()
            .iter()
            .map(|w| w["workspaceId"].as_str().unwrap())
            .collect();
        assert_eq!(ids, vec!["alpha", "beta"]);
        std::fs::remove_dir_all(&base).unwrap();
    }

    #[test]
    fn clear_removes_only_environment_contents() {
        let base = temp_base("clear");
        let ws = base.join("workspaces").join("default");
        write(&env_dir_in(&base, "default").join("run/a.txt"), "aaa");
        write(&env_dir_in(&base, "default").join("b.txt"), "bb");
        write(&ws.join("sessions").join("conversation.md"), "keep-me");
        write(&ws.join("files").join("artifact.md"), "keep-me-too");

        let r = clear_in(&base).unwrap();
        assert_eq!(r["freedBytes"], 5);
        assert_eq!(r["freedFiles"], 2);
        assert!(r["failed"].as_array().unwrap().is_empty());

        // environment 目录本身保留，内容为空
        assert!(env_dir_in(&base, "default").is_dir());
        assert_eq!(stat_dir(&env_dir_in(&base, "default")).files, 0);
        // 对话记录与工作产物原样存活
        assert_eq!(
            std::fs::read_to_string(ws.join("sessions").join("conversation.md")).unwrap(),
            "keep-me"
        );
        assert_eq!(
            std::fs::read_to_string(ws.join("files").join("artifact.md")).unwrap(),
            "keep-me-too"
        );
        std::fs::remove_dir_all(&base).unwrap();
    }

    #[test]
    fn clear_is_idempotent_and_empty_safe() {
        let base = temp_base("clear-idempotent");
        // 无 workspaces 目录时清理不报错、释放 0
        let r = clear_in(&base).unwrap();
        assert_eq!(r["freedBytes"], 0);
        assert!(r["cleared"].as_array().unwrap().is_empty());

        write(&env_dir_in(&base, "default").join("a.txt"), "aaa");
        assert_eq!(clear_in(&base).unwrap()["freedBytes"], 3);
        assert_eq!(clear_in(&base).unwrap()["freedBytes"], 0);
        std::fs::remove_dir_all(&base).unwrap();
    }

    #[test]
    fn clear_succeeds_without_any_workspace_env_dir() {
        let base = temp_base("clear-noenv");
        write(&base.join("workspaces").join("default").join("files").join("a.txt"), "x");
        let r = clear_in(&base).unwrap();
        assert_eq!(r["freedFiles"], 0);
        assert!(base.join("workspaces").join("default").join("files").join("a.txt").exists());
        std::fs::remove_dir_all(&base).unwrap();
    }

    #[test]
    fn guard_rejects_paths_outside_environment_layout() {
        let base = temp_base("guard");
        let ws_root = base.join("workspaces").join("default");
        std::fs::create_dir_all(&ws_root).unwrap();

        // workspaces/{id} 本身越界
        let err = guard_env_path(&base, &ws_root).unwrap_err();
        assert!(err.starts_with("SANDBOX_ENV_PATH_OUT_OF_SCOPE"), "{err}");
        // workspaces/{id}/files 越界
        let files_dir = ws_root.join("files");
        std::fs::create_dir_all(&files_dir).unwrap();
        assert!(guard_env_path(&base, &files_dir)
            .unwrap_err()
            .starts_with("SANDBOX_ENV_PATH_OUT_OF_SCOPE"));
        // base 之外越界
        assert!(guard_env_path(&base, base.parent().unwrap())
            .unwrap_err()
            .contains("OUT_OF_SCOPE"));
        // 正解：workspaces/{id}/environment
        let env = env_dir_in(&base, "default");
        std::fs::create_dir_all(&env).unwrap();
        assert!(guard_env_path(&base, &env).is_ok());
        std::fs::remove_dir_all(&base).unwrap();
    }

    #[test]
    fn guard_reports_missing_base_or_target() {
        let base = temp_base("guard-missing");
        assert!(guard_env_path(&base, &base.join("nope"))
            .unwrap_err()
            .starts_with("SANDBOX_ENV_PATH_INVALID"));
        let absent = base.join("absent");
        assert!(guard_env_path(&absent, &absent)
            .unwrap_err()
            .starts_with("SANDBOX_ENV_BASE_MISSING"));
        std::fs::remove_dir_all(&base).unwrap();
    }

    #[test]
    fn first_failure_surfaces_error_code() {
        assert_eq!(first_failure(&json!({ "failed": [] })), None);
        let v = json!({ "failed": [{ "error": "SANDBOX_ENV_CLEAR_FAILED: busy" }] });
        assert_eq!(
            first_failure(&v).as_deref(),
            Some("SANDBOX_ENV_CLEAR_FAILED: busy")
        );
    }
}
