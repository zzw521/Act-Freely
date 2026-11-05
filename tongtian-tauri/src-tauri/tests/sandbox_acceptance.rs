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

use std::io::Write;
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

/// Hook 套壳形态的真机验收：命令行里没有 `--workspace`，脚本目录只给只读
///
/// 钉住三件 Hooks 依赖的系统事实（`align::hooks::run_command` 走的就是这条路径）：
/// - 事件 JSON 能从父进程管道穿过启动器到达容器内脚本，stdout 能穿回来；
/// - 只读目录读得到、写不进去（越界写由 ACL 拒绝，不是策略层提示）；
/// - 唯一可写处是沙箱临时目录。
#[test]
fn hook_command_gets_stdio_and_only_writes_sandbox_temp() {
    let Some(python) = python() else {
        eprintln!("跳过：本机没有 python，无法执行 Hook 套壳验收");
        return;
    };

    let root = temp_root("hooks");
    let script_dir = root.join("scripts");
    let home = root.join("home");
    let work = root.join("work");
    for dir in [&script_dir, &home, &work] {
        std::fs::create_dir_all(dir).unwrap();
    }
    std::fs::write(home.join("tongtian.db"), "SECRET_KEY_SHOULD_NOT_LEAK").unwrap();

    let script = script_dir.join("probe.py");
    let outside = script_dir.join("evil.txt");
    let body = format!(
        r#"import json
import os
import sys

raw = sys.stdin.read()
event = ""
try:
    event = json.loads(raw).get("hook_event_name", "")
except Exception:
    event = ""

result = {{"event": event, "script_read": "no"}}

try:
    with open(r"{script}", "r", encoding="utf-8") as f:
        result["script_read"] = "allowed" if len(f.read()) > 0 else "empty"
except Exception as e:
    result["script_read"] = "denied:" + type(e).__name__

try:
    open(r"{outside}", "w", encoding="utf-8").write("evil")
    result["readonly_write"] = "allowed"
except Exception as e:
    result["readonly_write"] = "denied:" + type(e).__name__

temp = os.environ.get("TEMP") or ""
try:
    probe = os.path.join(temp, "tt-hook-temp-probe.txt")
    open(probe, "w", encoding="utf-8").write("ok")
    result["temp_write"] = "allowed"
    os.remove(probe)
except Exception as e:
    result["temp_write"] = "denied:" + type(e).__name__

print(json.dumps(result, ensure_ascii=True))
"#,
        script = script.display(),
        outside = outside.display(),
    );
    std::fs::write(&script, body).unwrap();

    let exe = env!("CARGO_BIN_EXE_tongtian-tauri");
    let python_dir = python.parent().unwrap().to_string_lossy().to_string();
    let mut child = Command::new(exe)
        .arg("--tt-sandbox-run")
        .arg("--network")
        .arg("off")
        .arg("--read-dir")
        .arg(&script_dir)
        .arg("--")
        .arg(&python)
        .arg(&script)
        // 数据根指向本用例的临时目录：不能把拒绝访问的 ACL 落到用户真实目录上
        .env("TONGTIAN_HOME", &home)
        .env("TONGTIANWORK_HOME", &work)
        .env("TT_SANDBOX_RUNTIME_DIRS", python_dir)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .expect("启动沙箱失败");
    // Hook 的事件正文走 stdin，脚本回读后由启动器把 stdout 原样交回父进程
    child
        .stdin
        .take()
        .expect("拿不到标准输入")
        .write_all(br#"{"hook_event_name":"PreToolUse"}"#)
        .expect("写入事件 JSON 失败");
    let out = child.wait_with_output().expect("等待沙箱进程失败");

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

    assert_eq!(value["event"], "PreToolUse", "事件 JSON 必须穿过启动器到达脚本");
    assert_eq!(value["script_read"], "allowed", "只读授权的脚本目录必须可读");
    assert_eq!(
        value["readonly_write"], "denied:PermissionError",
        "只给只读的目录绝不能写"
    );
    assert_eq!(value["temp_write"], "allowed", "沙箱临时目录必须可写");
    assert!(
        !outside.exists(),
        "只读目录里不应留下文件"
    );

    let _ = std::fs::remove_dir_all(&root);
}
