//! 系统级沙箱真机验收（Windows + 本机 Python）
//!
//! 验证的是**系统事实**，不是策略层：
//! - 工作区内写入成功
//! - 工作区外写入被 ACL 拒绝
//! - 数据根中的会话库读取被拒绝
//! - 断网时公网连接被拒绝
//!
//! 没有 Python 的机器直接跳过，不把环境缺失伪装成通过。

#![cfg(windows)]

use std::path::{Path, PathBuf};
use std::process::Command;

fn python() -> Option<PathBuf> {
    for name in ["python.exe", "python"] {
        let out = Command::new("where").arg(name).output();
        if let Ok(out) = out {
            if out.status.success() {
                let text = String::from_utf8_lossy(&out.stdout);
                if let Some(first) = text.lines().find(|l| !l.trim().is_empty()) {
                    let p = PathBuf::from(first.trim());
                    if p.is_file() {
                        return Some(p);
                    }
                }
            }
        }
    }
    None
}

fn temp_root(tag: &str) -> PathBuf {
    let base = std::env::temp_dir().join(format!(
        "tt-sandbox-test-{tag}-{}",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&base);
    std::fs::create_dir_all(&base).unwrap();
    base
}

fn write_probe(dir: &Path, outside: &Path, secret: &Path) -> PathBuf {
    let script = dir.join("probe.py");
    let body = format!(
        r#"import json
import socket
import sys

result = {{}}
try:
    open(r"{}", "w", encoding="utf-8").write("ok")
    result["workspace_write"] = "allowed"
except Exception as e:
    result["workspace_write"] = "denied:" + type(e).__name__

try:
    open(r"{}", "w", encoding="utf-8").write("evil")
    result["outside_write"] = "allowed"
except Exception as e:
    result["outside_write"] = "denied:" + type(e).__name__

try:
    open(r"{}", "rb").read(8)
    result["data_root_read"] = "allowed"
except Exception as e:
    result["data_root_read"] = "denied:" + type(e).__name__

try:
    socket.create_connection(("223.5.5.5", 53), timeout=4).close()
    result["network"] = "allowed"
except Exception:
    result["network"] = "denied"

print(json.dumps(result, ensure_ascii=True))
"#,
        dir.join("evidence.txt").display(),
        outside.join("evil.txt").display(),
        secret.display(),
    );
    std::fs::write(&script, body).unwrap();
    script
}

#[test]
fn appcontainer_blocks_outside_write_and_secret_read() {
    let Some(python) = python() else {
        eprintln!("跳过：本机没有 python，无法执行沙箱验收");
        return;
    };

    let root = temp_root("accept");
    let workspace = root.join("ws");
    let outside = root.join("outside");
    let home = root.join("home");
    for dir in [&workspace, &outside, &home] {
        std::fs::create_dir_all(dir).unwrap();
    }
    let secret = home.join("tongtian.db");
    std::fs::write(&secret, "SECRET_KEY_SHOULD_NOT_LEAK").unwrap();
    let script = write_probe(&workspace, &outside, &secret);

    let exe = env!("CARGO_BIN_EXE_tongtian-tauri");
    let python_dir = python.parent().unwrap().to_string_lossy().to_string();
    let out = Command::new(exe)
        .arg("--tt-sandbox-run")
        .arg("--workspace")
        .arg(&workspace)
        .arg("--network")
        .arg("off")
        .arg("--")
        .arg(&python)
        .arg(&script)
        .env("TONGTIAN_HOME", &home)
        .env("TT_SANDBOX_RUNTIME_DIRS", python_dir)
        .stdin(std::process::Stdio::null())
        .output()
        .expect("启动沙箱失败");

    let stdout = String::from_utf8_lossy(&out.stdout);
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        out.status.success(),
        "沙箱启动器退出码 {:?}\nstdout={stdout}\nstderr={stderr}",
        out.status.code()
    );
    let line = stdout
        .lines()
        .find(|l| l.trim_start().starts_with('{'))
        .unwrap_or_else(|| panic!("没有拿到探测结果：stdout={stdout} stderr={stderr}"));
    let value: serde_json::Value = serde_json::from_str(line).expect("探测结果不是 JSON");

    assert_eq!(value["workspace_write"], "allowed", "工作区内写入应成功");
    assert_eq!(
        value["outside_write"], "denied:PermissionError",
        "工作区外写入必须被系统拒绝"
    );
    assert_eq!(
        value["data_root_read"], "denied:PermissionError",
        "数据根会话库必须不可读"
    );
    assert_eq!(value["network"], "denied", "断网时不得访问公网");
    assert!(
        !outside.join("evil.txt").exists(),
        "越界文件不应被创建"
    );

    let _ = std::fs::remove_dir_all(&root);
}
