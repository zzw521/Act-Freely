//! Hooks 配置持久化与真实试运行（千问对标整改 spec §5.1 / §6.3）
//!
//! 语义与整改基准实现（Electron 版 HookRunner）逐条对齐：
//! - 命令经系统 shell 解释执行，stdin 收事件 JSON，退出码 0=成功 / 2=阻塞
//! - `PreToolUse` / `Stop` / `SubagentStop` 三事件具备阻塞语义
//! - `timeout` 单位为秒，缺省 60，上限 600（超时后杀进程树并标记 timed_out）
//!
//! 配置落 `hook_configs` 表（id 固定 'global'），与渲染层 `HookConfigStore` 同构。

use std::io::{Read, Write};
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant};

use rusqlite::OptionalExtension;
use serde_json::{json, Value};

use crate::store::Store;

/// 配置行主键（全局唯一一份）
pub const GLOBAL_ID: &str = "global";

/// 支持的事件名（与 contracts.ts 的 HookEventName 逐一同步）
pub const ALL_EVENTS: &[&str] = &[
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "Notification",
    "PermissionRequest",
];

/// 支持 exit 2 阻塞语义的事件
pub const BLOCKABLE_EVENTS: &[&str] = &["PreToolUse", "Stop", "SubagentStop"];

pub const DEFAULT_TIMEOUT_SEC: u64 = 60;
pub const MAX_TIMEOUT_SEC: u64 = 600;

/// 单次 hook 命令执行结果（对应 contracts.ts HookResult）
pub struct HookRun {
    pub exit_code: i64,
    pub stdout: String,
    pub stderr: String,
    pub blocked: bool,
    pub timed_out: bool,
}

impl HookRun {
    pub fn to_value(&self) -> Value {
        json!({
            "exitCode": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "blocked": self.blocked,
            "timedOut": self.timed_out,
        })
    }
}

// ── 配置校验与读写 ────────────────────────────────────────────

fn is_blockable(event: &str) -> bool {
    BLOCKABLE_EVENTS.contains(&event)
}

/// 校验 `HookConfigStore`：事件名合法、命令非空、超时为正且封顶
/// 非法即拒（返回错误码前缀串），不做静默丢弃或降级
pub fn validate_config(config: &Value) -> Result<(), String> {
    let hooks = match config.get("hooks").or(Some(config)) {
        Some(v) => v,
        None => return Err("HOOK_CONFIG_INVALID: 缺少 hooks 字段".to_string()),
    };
    let map = hooks
        .as_object()
        .ok_or_else(|| "HOOK_CONFIG_INVALID: hooks 必须是事件名到分组数组的对象".to_string())?;

    for (event, groups) in map {
        if !ALL_EVENTS.contains(&event.as_str()) {
            return Err(format!(
                "HOOK_INVALID_EVENT: 不支持的 Hook 事件「{event}」，可选值：{}",
                ALL_EVENTS.join(" / ")
            ));
        }
        let groups = groups
            .as_array()
            .ok_or_else(|| format!("HOOK_INVALID_GROUP: 事件「{event}」的值必须是分组建制数组"))?;
        for group in groups {
            if let Some(m) = group.get("matcher") {
                if !m.is_null() && m.as_str().is_none() {
                    return Err(format!("HOOK_INVALID_MATCHER: 事件「{event}」的 matcher 必须是字符串"));
                }
            }
            let cmds = group
                .get("hooks")
                .and_then(|v| v.as_array())
                .ok_or_else(|| format!("HOOK_INVALID_COMMAND: 事件「{event}」的分组缺少 hooks 数组"))?;
            for cmd in cmds {
                let command = cmd.get("command").and_then(|v| v.as_str()).unwrap_or("").trim();
                if command.is_empty() {
                    return Err(format!("HOOK_INVALID_COMMAND: 事件「{event}」存在空命令"));
                }
                if let Some(t) = cmd.get("timeout") {
                    if !t.is_null() {
                        let secs = t.as_u64().ok_or_else(|| {
                            format!("HOOK_INVALID_TIMEOUT: 事件「{event}」的 timeout 必须是正整数秒")
                        })?;
                        if secs == 0 || secs > MAX_TIMEOUT_SEC {
                            return Err(format!(
                                "HOOK_INVALID_TIMEOUT: 事件「{event}」的 timeout 需在 1~{MAX_TIMEOUT_SEC} 秒之间"
                            ));
                        }
                    }
                }
            }
        }
    }
    Ok(())
}

/// 归一化为可比较的精简视图（去掉 null/空串，便于跨端断言）
fn normalize_hooks(config: &Value) -> Value {
    let hooks = config.get("hooks").unwrap_or(config);
    match hooks.as_object() {
        Some(_) => hooks.clone(),
        None => json!({}),
    }
}

/// 读取 Hook 配置（无配置行时返回空对象，不伪造已保存数据）
pub fn get(store: &Store) -> Result<Value, String> {
    let row = store.with_conn(|c| {
        c.query_row(
            "SELECT hooks_json FROM hook_configs WHERE id = ?1",
            [GLOBAL_ID],
            |r| r.get::<_, String>(0),
        )
        .optional()
        .map_err(|e| e.to_string())
    })?;
    let hooks: Value = match row {
        Some(raw) => serde_json::from_str(&raw).unwrap_or_else(|_| json!({})),
        None => json!({}),
    };
    Ok(json!({ "hooks": hooks }))
}

/// 保存 Hook 配置（upsert，覆盖式写入用户刚提交的配置）
pub fn set(store: &Store, config: &Value, now_ms: i64) -> Result<(), String> {
    validate_config(config)?;
    let hooks = normalize_hooks(config);
    let raw = serde_json::to_string(&hooks).map_err(|e| format!("HOOK_SAVE_FAILED: {e}"))?;
    store.with_conn(|c| {
        c.execute(
            "INSERT INTO hook_configs (id, hooks_json, updated_at_ms) VALUES (?1, ?2, ?3)
             ON CONFLICT(id) DO UPDATE SET hooks_json = excluded.hooks_json,
                                           updated_at_ms = excluded.updated_at_ms",
            rusqlite::params![GLOBAL_ID, raw, now_ms],
        )
        .map(|_| ())
        .map_err(|e| format!("HOOK_SAVE_FAILED: {e}"))
    })
}

/// 事件总数（用于保存后审计摘要）
pub fn count_events(config: &Value) -> usize {
    normalize_hooks(config)
        .as_object()
        .map(|m| {
            m.iter()
                .filter(|(_, groups)| groups.as_array().map(|a| !a.is_empty()).unwrap_or(false))
                .count()
        })
        .unwrap_or(0)
}

// ── 真实执行 ──────────────────────────────────────────────────

/// 展开 `~`（对齐整改基准：`~/x` → `<home>/x`）
fn expand_home(cmd: &str) -> String {
    let trimmed = cmd.trim_start();
    if trimmed != "~" && !trimmed.starts_with("~/") && !trimmed.starts_with("~\\") {
        return cmd.to_string();
    }
    let home = std::env::var("USERPROFILE")
        .or_else(|_| std::env::var("HOME"))
        .unwrap_or_default();
    if home.is_empty() {
        return cmd.to_string();
    }
    if trimmed == "~" {
        return home;
    }
    format!("{}{}", home, &trimmed[1..])
}

/// 把子进程管道读到字符串（独立线程，避免读满管道阻塞等待）
fn spawn_drain<R: Read + Send + 'static>(mut reader: R) -> mpsc::Receiver<String> {
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = reader.read_to_end(&mut buf);
        let _ = tx.send(String::from_utf8_lossy(&buf).to_string());
    });
    rx
}

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// 超时后终止整棵进程树（Windows 下 cmd 的子进程需 taskkill /T 才能一并回收）
fn kill_tree(child: &mut std::process::Child) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let _ = Command::new("taskkill")
            .args(["/pid", &child.id().to_string(), "/T", "/F"])
            .creation_flags(CREATE_NO_WINDOW)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
    let _ = child.kill();
}

/// 试运行单条 hook 命令：真实拉起进程、stdin 喂事件 JSON、按秒数超时
///
/// 阻塞语义：仅在具备阻塞能力的事件上把 exit 2 记为 `blocked`，
/// 与整改基准的事件模型一致，避免把「脚本自身返回 2」误报成阻塞。
pub fn run_command(event: &str, command: &str, timeout_sec: u64, input: &Value) -> HookRun {
    let shell_cmd = expand_home(command);
    if shell_cmd.trim().is_empty() {
        return HookRun {
            exit_code: 1,
            stdout: String::new(),
            stderr: "HOOK_INVALID_COMMAND: 命令为空".to_string(),
            blocked: false,
            timed_out: false,
        };
    }

    #[cfg(windows)]
    let mut spec = {
        use std::os::windows::process::CommandExt;
        let mut c = Command::new("cmd");
        c.args(["/C", shell_cmd.as_str()])
            .creation_flags(CREATE_NO_WINDOW);
        c
    };
    #[cfg(not(windows))]
    let mut spec = {
        let mut c = Command::new("/bin/sh");
        c.args(["-c", shell_cmd.as_str()]);
        c
    };

    let mut child = match spec
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
    {
        Ok(c) => c,
        Err(e) => {
            return HookRun {
                exit_code: 1,
                stdout: String::new(),
                stderr: format!("HOOK_TEST_FAILED: 命令启动失败：{e}"),
                blocked: false,
                timed_out: false,
            };
        }
    };

    // stdin 传事件 JSON（管道已建立，写失败说明脚本没读，忽略即可）
    if let Some(mut stdin) = child.stdin.take() {
        let payload = input.to_string();
        let _ = stdin.write_all(payload.as_bytes());
    }
    let stdout_rx = child.stdout.take().map(spawn_drain);
    let stderr_rx = child.stderr.take().map(spawn_drain);

    let secs = timeout_sec.clamp(1, MAX_TIMEOUT_SEC);
    let deadline = Instant::now() + Duration::from_secs(secs);
    let mut timed_out = false;
    let status = loop {
        match child.try_wait() {
            Ok(Some(st)) => break Some(st),
            Ok(None) => {
                if Instant::now() >= deadline {
                    timed_out = true;
                    kill_tree(&mut child);
                    break child.wait().ok();
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            Err(_) => {
                timed_out = true;
                kill_tree(&mut child);
                break None;
            }
        }
    };

    // 进程已退出/被杀，管道应在毫秒级 EOF；留 2 秒宽限避免残留句柄卡住调用方
    let grace = Duration::from_secs(2);
    let stdout = stdout_rx
        .and_then(|rx| rx.recv_timeout(grace).ok())
        .unwrap_or_default();
    let stderr_raw = stderr_rx
        .and_then(|rx| rx.recv_timeout(grace).ok())
        .unwrap_or_default();

    let exit_code = if timed_out {
        1
    } else {
        status.and_then(|s| s.code()).unwrap_or(1) as i64
    };
    let stderr = if timed_out {
        format!(
            "{}超时被终止（{secs} 秒上限）",
            if stderr_raw.is_empty() {
                String::new()
            } else {
                format!("{stderr_raw}\n")
            }
        )
    } else {
        stderr_raw
    };

    HookRun {
        exit_code,
        stdout,
        stderr,
        blocked: is_blockable(event) && exit_code == 2,
        timed_out,
    }
}

/// 构造试运行的事件输入（对齐 contracts.ts HookInput）
pub fn test_input(event: &str, cwd: &str) -> Value {
    json!({
        "session_id": "test-session",
        "cwd": cwd,
        "hook_event_name": event,
    })
}

/// 事件名合法性校验（command 入口用，非法即拒）
pub fn ensure_event(event: &str) -> Result<(), String> {
    if ALL_EVENTS.contains(&event) {
        Ok(())
    } else {
        Err(format!(
            "HOOK_INVALID_EVENT: 不支持的 Hook 事件「{event}」，可选值：{}",
            ALL_EVENTS.join(" / ")
        ))
    }
}

/// 命令与超时校验（试运行入口用）
pub fn ensure_command(command: &str) -> Result<(), String> {
    if command.trim().is_empty() {
        return Err("HOOK_INVALID_COMMAND: 试运行需要非空命令".to_string());
    }
    Ok(())
}

pub fn clamp_timeout(timeout: Option<u64>) -> u64 {
    timeout.unwrap_or(DEFAULT_TIMEOUT_SEC).clamp(1, MAX_TIMEOUT_SEC)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn temp_store(name: &str) -> (Store, PathBuf) {
        let dir = std::env::temp_dir().join(format!(
            "tt-hooks-{name}-{}",
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let store = Store::open(dir.join("t.db")).unwrap();
        (store, dir)
    }

    // ── 配置读写 ──────────────────────────────────────────

    #[test]
    fn test_get_returns_empty_when_never_saved() {
        let (store, dir) = temp_store("get-empty");
        let v = get(&store).unwrap();
        assert_eq!(v["hooks"], json!({}), "未保存过不得伪造配置");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_set_get_roundtrip() {
        let (store, dir) = temp_store("roundtrip");
        let cfg = json!({
            "hooks": {
                "PreToolUse": [{
                    "matcher": "Bash|Write",
                    "hooks": [{"type": "command", "command": "echo hi", "timeout": 5}]
                }]
            }
        });
        set(&store, &cfg, 1234).unwrap();
        let back = get(&store).unwrap();
        assert_eq!(back["hooks"]["PreToolUse"][0]["matcher"], "Bash|Write");
        assert_eq!(back["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "echo hi");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_set_overwrites_previous_config() {
        let (store, dir) = temp_store("overwrite");
        set(&store, &json!({"hooks": {"Stop": [{"hooks": [{"command": "a"}]}]}}), 1).unwrap();
        set(&store, &json!({"hooks": {"Stop": [{"hooks": [{"command": "b"}]}]}}), 2).unwrap();
        let back = get(&store).unwrap();
        assert_eq!(back["hooks"]["Stop"][0]["hooks"][0]["command"], "b");
        let rows: i64 = store
            .with_conn(|c| c.query_row("SELECT COUNT(*) FROM hook_configs", [], |r| r.get(0)).map_err(|e| e.to_string()))
            .unwrap();
        assert_eq!(rows, 1, "全局配置应只有一行（upsert 不追加历史）");
        let _ = std::fs::remove_dir_all(&dir);
    }

    // ── 校验分支 ──────────────────────────────────────────

    #[test]
    fn test_validate_rejects_unknown_event() {
        let err = validate_config(&json!({"hooks": {"NotAnEvent": []}})).unwrap_err();
        assert!(err.starts_with("HOOK_INVALID_EVENT"), "{err}");
    }

    #[test]
    fn test_validate_rejects_empty_command() {
        let err = validate_config(&json!({
            "hooks": {"Stop": [{"hooks": [{"command": "   "}]}]}
        }))
        .unwrap_err();
        assert!(err.starts_with("HOOK_INVALID_COMMAND"), "{err}");
    }

    #[test]
    fn test_validate_rejects_group_without_hooks_array() {
        let err = validate_config(&json!({"hooks": {"Stop": [{"matcher": "x"}]}})).unwrap_err();
        assert!(err.starts_with("HOOK_INVALID_COMMAND"), "{err}");
    }

    #[test]
    fn test_validate_rejects_out_of_range_timeout() {
        for secs in [0u64, 9999] {
            let err = validate_config(&json!({
                "hooks": {"Stop": [{"hooks": [{"command": "x", "timeout": secs}]}]}
            }))
            .unwrap_err();
            assert!(err.starts_with("HOOK_INVALID_TIMEOUT"), "{secs} → {err}");
        }
    }

    #[test]
    fn test_validate_accepts_missing_matcher_and_null_timeout() {
        validate_config(&json!({
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "x"}]}],
                "Stop": [{"matcher": null, "hooks": [{"command": "y", "timeout": null}]}]
            }
        }))
        .unwrap();
    }

    #[test]
    fn test_count_events_ignores_empty_groups() {
        let cfg = json!({
            "hooks": {"Stop": [{"hooks": [{"command": "x"}]}], "PreToolUse": []}
        });
        assert_eq!(count_events(&cfg), 1);
        assert_eq!(count_events(&json!({"hooks": {}})), 0);
    }

    // ── 事件/命令入口校验 ─────────────────────────────────

    #[test]
    fn test_ensure_event_covers_all_contract_events() {
        assert_eq!(ALL_EVENTS.len(), 12);
        for e in ALL_EVENTS {
            ensure_event(e).unwrap();
        }
        assert!(ensure_event("").is_err());
        assert!(ensure_event("preToolUse").is_err(), "事件名大小写敏感");
    }

    #[test]
    fn test_ensure_command_and_timeout() {
        assert!(ensure_command("").is_err());
        assert!(ensure_command("echo ok").is_ok());
        assert_eq!(clamp_timeout(None), DEFAULT_TIMEOUT_SEC);
        assert_eq!(clamp_timeout(Some(0)), 1);
        assert_eq!(clamp_timeout(Some(9999)), MAX_TIMEOUT_SEC);
    }

    // ── 路径展开 ──────────────────────────────────────────

    #[test]
    fn test_expand_home_only_on_tilde() {
        assert_eq!(expand_home("./local.sh"), "./local.sh");
        assert_eq!(expand_home(""), "");
        let home = std::env::var("USERPROFILE")
            .or_else(|_| std::env::var("HOME"))
            .unwrap_or_default();
        if !home.is_empty() {
            assert_eq!(expand_home("~"), home);
            assert_eq!(expand_home("~/a.sh"), format!("{home}/a.sh"));
        }
    }

    #[test]
    fn test_test_input_shape_matches_contract() {
        let v = test_input("Stop", "d:/cwd");
        assert_eq!(v["session_id"], "test-session");
        assert_eq!(v["cwd"], "d:/cwd");
        assert_eq!(v["hook_event_name"], "Stop");
    }

    // ── 真实执行（本机会拉起子进程，不落任何业务数据） ────

    #[test]
    fn test_run_command_captures_stdout_and_zero_exit() {
        let run = run_command(
            "SessionStart",
            if cfg!(windows) { "echo HOOK_OK" } else { "echo HOOK_OK" },
            20,
            &test_input("SessionStart", "."),
        );
        assert!(!run.timed_out, "stderr={}", run.stderr);
        assert_eq!(run.exit_code, 0, "stdout={} stderr={}", run.stdout, run.stderr);
        assert!(run.stdout.contains("HOOK_OK"), "stdout={}", run.stdout);
        assert!(!run.blocked);
    }

    #[test]
    fn test_run_command_reads_stdin_json() {
        // stdin 必须真的喂到脚本：用独立脚本文件回读（内联 -e 会被 cmd 的引号规则改写）
        if which("node").is_none() {
            return; // 无 node 时跳过（不是引擎能力缺失，只是本机工具缺失）
        }
        let dir = std::env::temp_dir().join(format!(
            "tt-hooks-stdin-{}",
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let script = dir.join("echo_stdin.js");
        std::fs::write(
            &script,
            "let s='';process.stdin.on('data',d=>s+=d).on('end',()=>process.stdout.write(s));",
        )
        .unwrap();

        let run = run_command(
            "PreToolUse",
            // 临时目录无空格：cmd /C 下再套引号会把引号本身传给 node 的路径
            &format!("node {}", script.display()),
            30,
            &json!({"hook_event_name": "PreToolUse", "tool_name": "Bash"}),
        );
        let _ = std::fs::remove_dir_all(&dir);

        assert_eq!(run.exit_code, 0, "stderr={}", run.stderr);
        assert!(run.stdout.contains("tool_name"), "stdout={}", run.stdout);
        assert!(run.stdout.contains("PreToolUse"), "stdout={}", run.stdout);
    }

    #[test]
    fn test_run_command_nonzero_exit_is_reported_not_swallowed() {
        let run = run_command(
            "Stop",
            if cfg!(windows) { "exit /b 3" } else { "exit 3" },
            20,
            &test_input("Stop", "."),
        );
        assert_eq!(run.exit_code, 3);
        assert!(!run.blocked, "exit 3 不是阻塞语义");
    }

    #[test]
    fn test_run_command_exit_two_blocks_only_blockable_events() {
        let stop = run_command("Stop", if cfg!(windows) { "exit /b 2" } else { "exit 2" }, 20, &test_input("Stop", "."));
        assert_eq!(stop.exit_code, 2);
        assert!(stop.blocked, "Stop 属可阻塞事件");

        let start = run_command("SessionStart", if cfg!(windows) { "exit /b 2" } else { "exit 2" }, 20, &test_input("SessionStart", "."));
        assert_eq!(start.exit_code, 2);
        assert!(!start.blocked, "SessionStart 无阻塞语义");
    }

    #[test]
    fn test_run_command_timeout_kills_process() {
        let run = run_command(
            "Stop",
            if cfg!(windows) { "ping -n 30 127.0.0.1 >nul & exit /b 0" } else { "sleep 30" },
            1,
            &test_input("Stop", "."),
        );
        assert!(run.timed_out, "应超时（exit={} stderr={}）", run.exit_code, run.stderr);
        assert_eq!(run.exit_code, 1, "超时按失败计");
        assert!(run.stderr.contains("超时"), "stderr={}", run.stderr);
    }

    #[test]
    fn test_run_command_rejects_empty_command() {
        let run = run_command("Stop", "   ", 5, &test_input("Stop", "."));
        assert_eq!(run.exit_code, 1);
        assert!(run.stderr.starts_with("HOOK_INVALID_COMMAND"), "{}", run.stderr);
    }

    fn which(bin: &str) -> Option<std::path::PathBuf> {
        std::env::var("PATH")
            .ok()?
            .split(if cfg!(windows) { ';' } else { ':' })
            .map(|p| std::path::Path::new(p).join(if cfg!(windows) { format!("{bin}.exe") } else { bin.to_string() }))
            .find(|p| p.exists())
    }
}
