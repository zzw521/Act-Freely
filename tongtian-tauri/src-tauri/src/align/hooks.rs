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

// ── 执行面：放行台账 + 事件编排 ────────────────────────────────

/// 引擎执行链路里真实存在触发节点的事件（会话/工具事件在 `engine/hooks_runtime.py`，
/// 子 Agent 事件在 `engine/subagent_runtime.py`；由契约测试锁定：
/// 页面只能宣称这里列出的事件会真的触发）
pub const FIRED_EVENTS: &[&str] = &[
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PreCompact",
    "Stop",
    "SubagentStart",
    "SubagentStop",
];

/// 配置得了、但当前引擎没有触发点的事件（不假装会触发，页面与事件流都要说出来）
pub const UNFIRED_EVENTS: &[&str] = &["Notification", "PermissionRequest"];

/// 用工具名做 matcher 比对的事件；其余事件按 `match_target` 取输入里的对应字段
pub const TOOL_EVENTS: &[&str] = &["PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest"];

fn normalize_cmd(command: &str) -> String {
    command.trim().to_string()
}

/// matcher 语义：空 / `*` 匹配全部，其余按正则（不锚定）匹配
/// 正则写错必须如实报错，不能静默当成「不匹配」——否则用户会以为 Hook 没触发是引擎的问题
pub fn matcher_matches(matcher: Option<&str>, target: Option<&str>) -> Result<bool, String> {
    let m = matcher.map(str::trim).filter(|s| !s.is_empty() && *s != "*");
    let Some(pattern) = m else {
        return Ok(true);
    };
    let Some(text) = target else {
        // 没有可比对的字段（如 UserPromptSubmit）：matcher 不参与判定
        return Ok(true);
    };
    let re = regex::Regex::new(pattern)
        .map_err(|e| format!("HOOK_INVALID_MATCHER: matcher「{pattern}」不是合法正则：{e}"))?;
    Ok(re.is_match(text))
}

/// 非工具事件的 matcher 比对字段（与引擎 `hooks_runtime.py` 下发的 payload 键一致）
fn match_target(event: &str, tool: Option<&str>, input: &Value) -> Option<String> {
    if TOOL_EVENTS.contains(&event) {
        return tool.map(str::to_string);
    }
    let key = match event {
        "SessionStart" => "source",
        "SessionEnd" => "reason",
        "PreCompact" => "trigger",
        "Notification" => "type",
        "SubagentStart" | "SubagentStop" => "agent_type",
        _ => return None,
    };
    input
        .get(key)
        .and_then(|v| v.as_str())
        .map(str::to_string)
        .or_else(|| Some(String::new()))
}

/// 放行一条 Hook 命令（幂等：重复放行只刷新时间，不产生多行）
pub fn grant(store: &Store, command: &str, event: &str, now_ms: i64) -> Result<(), String> {
    let cmd = normalize_cmd(command);
    if cmd.is_empty() {
        return Err("HOOK_INVALID_COMMAND: 空命令不需要放行".to_string());
    }
    ensure_event(event)?;
    store.with_conn(|c| {
        c.execute(
            "INSERT INTO hook_grants (command, event, granted_at_ms) VALUES (?1, ?2, ?3) \
             ON CONFLICT(command) DO UPDATE SET event = excluded.event, \
                                                 granted_at_ms = excluded.granted_at_ms",
            rusqlite::params![cmd, event, now_ms],
        )
        .map(|_| ())
        .map_err(|e| format!("HOOK_GRANT_FAILED: {e}"))
    })
}

/// 撤销放行：只删授权记录，`hook_configs` 里的配置原样保留（撤权 ≠ 删配置）
pub fn revoke(store: &Store, command: &str) -> Result<(), String> {
    let cmd = normalize_cmd(command);
    store.with_conn(|c| {
        c.execute("DELETE FROM hook_grants WHERE command = ?1", [cmd.as_str()])
            .map(|_| ())
            .map_err(|e| format!("HOOK_REVOKE_FAILED: {e}"))
    })
}

/// 配置里已经不再包含的命令，其放行随之作废，返回被作废的命令清单。
/// 不做这一步会漏：用户删掉某条 Hook、再原样加回来，就绕过了「重新放行」这一步。
/// 只清 `hook_grants`（本轮刚写入的意愿记录），绝不碰 `hook_configs` 里的存量配置。
pub fn prune_grants(store: &Store, config: &Value) -> Result<Vec<String>, String> {
    let keep = configured_commands(config);
    let stale: Vec<String> = list_grants(store)?
        .into_iter()
        .filter_map(|g| g["command"].as_str().map(str::to_string))
        .filter(|c| !keep.iter().any(|k| k == c))
        .collect();
    for cmd in &stale {
        revoke(store, cmd)?;
    }
    Ok(stale)
}

pub fn is_granted(store: &Store, command: &str) -> Result<bool, String> {
    let cmd = normalize_cmd(command);
    store.with_conn(|c| {
        c.query_row(
            "SELECT COUNT(*) FROM hook_grants WHERE command = ?1",
            [cmd.as_str()],
            |r| r.get::<_, i64>(0),
        )
        .map(|n| n > 0)
        .map_err(|e| format!("HOOK_GRANT_QUERY_FAILED: {e}"))
    })
}

/// 授权台账（含执行次数），供 Hooks 页回显
pub fn list_grants(store: &Store) -> Result<Vec<Value>, String> {
    store.with_conn(|c| {
        let mut stmt = c
            .prepare(
                "SELECT command, event, granted_at_ms, run_count, last_run_at_ms \
                 FROM hook_grants ORDER BY granted_at_ms",
            )
            .map_err(|e| format!("HOOK_GRANT_QUERY_FAILED: {e}"))?;
        let rows = stmt
            .query_map([], |r| {
                Ok(json!({
                    "command": r.get::<_, String>(0)?,
                    "event": r.get::<_, String>(1)?,
                    "grantedAtMs": r.get::<_, i64>(2)?,
                    "runCount": r.get::<_, i64>(3)?,
                    "lastRunAtMs": r.get::<_, Option<i64>>(4)?,
                }))
            })
            .map_err(|e| format!("HOOK_GRANT_QUERY_FAILED: {e}"))?;
        rows.collect::<Result<Vec<Value>, _>>()
            .map_err(|e| format!("HOOK_GRANT_QUERY_FAILED: {e}"))
    })
}

fn mark_run(store: &Store, command: &str, now_ms: i64) {
    let _ = store.with_conn(|c| {
        c.execute(
            "UPDATE hook_grants SET run_count = run_count + 1, last_run_at_ms = ?2 \
             WHERE command = ?1",
            rusqlite::params![normalize_cmd(command), now_ms],
        )
        .map(|_| ())
        .map_err(|e| e.to_string())
    });
}

/// 配置里出现过的全部命令原文（去重，保持配置顺序）
pub fn configured_commands(config: &Value) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    let map = match normalize_hooks(config).as_object().cloned() {
        Some(m) => m,
        None => return out,
    };
    for groups in map.values() {
        for group in groups.as_array().cloned().unwrap_or_default() {
            for cmd in group
                .get("hooks")
                .and_then(|v| v.as_array())
                .cloned()
                .unwrap_or_default()
            {
                let raw = cmd.get("command").and_then(|v| v.as_str()).unwrap_or("").trim();
                if !raw.is_empty() && !out.iter().any(|c| c == raw) {
                    out.push(raw.to_string());
                }
            }
        }
    }
    out
}

/// 执行面概览：配置了几条、哪些事件有触发点、哪些命令还没放行
pub fn runtime_summary(store: &Store) -> Result<Value, String> {
    let config = get(store)?;
    let hooks = normalize_hooks(&config);
    let by_event = hooks.as_object().cloned().unwrap_or_default();
    let commands = configured_commands(&config);
    let mounted: Vec<&str> = ALL_EVENTS
        .iter()
        .copied()
        .filter(|e| {
            by_event
                .get(*e)
                .and_then(|v| v.as_array())
                .map(|a| !a.is_empty())
                .unwrap_or(false)
        })
        .collect();
    let mut ungranted: Vec<String> = Vec::new();
    for cmd in &commands {
        if !is_granted(store, cmd)? {
            ungranted.push(cmd.clone());
        }
    }
    // 用户确实配了、但引擎没有触发点的事件 —— 页面要逐条说明，不能让它看起来「配了就会跑」
    let mounted_unfired: Vec<&str> = mounted
        .iter()
        .filter(|e| UNFIRED_EVENTS.contains(e))
        .copied()
        .collect();
    Ok(json!({
        "configured": commands.len(),
        "mountedEvents": mounted,
        "firedEvents": FIRED_EVENTS,
        "unfiredEvents": UNFIRED_EVENTS,
        "mountedUnfired": mounted_unfired,
        "ungranted": ungranted,
    }))
}

/// 单条命令的执行台账（渲染成契约里的 `results[]` 元素）
fn outcome_value(cmd: &Value, run: Option<&HookRun>, granted: bool, err: Option<&str>) -> Value {
    let command = cmd.get("command").and_then(|v| v.as_str()).unwrap_or("").trim();
    match run {
        Some(r) => json!({
            "command": command,
            "executed": true,
            "granted": granted,
            "exitCode": r.exit_code,
            "stdout": r.stdout,
            "stderr": r.stderr,
            "blocked": r.blocked,
            "timedOut": r.timed_out,
        }),
        None => json!({
            "command": command,
            "executed": false,
            "granted": granted,
            "reason": err.unwrap_or(if granted { "未执行" } else { "未放行" }),
        }),
    }
}

/// 真实触发一个 Hook 事件：读 `hooks_get` 的同一份配置，按 matcher 过滤，
/// 已放行的命令走与试运行完全相同的 `run_command`；未放行的不执行但如实回报。
///
/// 返回 `blocked`（可阻塞事件 exit 2）与 `notes`（给引擎摊成事件流的中文说明）。
pub fn fire(
    store: &Store,
    event: &str,
    tool: Option<&str>,
    payload: &Value,
    now_ms: i64,
) -> Result<Value, String> {
    ensure_event(event)?;
    let config = get(store)?;
    let groups = config
        .get("hooks")
        .and_then(|h| h.get(event))
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    if groups.is_empty() {
        return Ok(json!({ "event": event, "results": [], "blocked": false, "notes": [] }));
    }

    let target = match_target(event, tool, payload);
    let input = real_input(event, tool, payload);
    let mut results: Vec<Value> = Vec::new();
    let mut notes: Vec<String> = Vec::new();
    let mut blocked = false;

    for group in &groups {
        let matcher = group.get("matcher").and_then(|v| v.as_str());
        match matcher_matches(matcher, target.as_deref()) {
            Ok(true) => {}
            Ok(false) => continue,
            Err(e) => {
                notes.push(format!("Hook「{event}」的 matcher 未生效：{}", e.split(": ").nth(1).unwrap_or(&e)));
                continue;
            }
        }
        let cmds = group
            .get("hooks")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();
        for cmd in &cmds {
            let command = cmd.get("command").and_then(|v| v.as_str()).unwrap_or("").trim();
            if command.is_empty() {
                continue;
            }
            let secs = clamp_timeout(cmd.get("timeout").and_then(|v| v.as_u64()));
            if !is_granted(store, command)? {
                results.push(outcome_value(cmd, None, false, None));
                notes.push(format!(
                    "Hook「{event}」命令 `{command}` 尚未放行，本轮没有执行（在 Hooks 页点「放行」后才会真的跑）"
                ));
                continue;
            }
            let run = run_command(event, command, secs, &input);
            mark_run(store, command, now_ms);
            blocked |= run.blocked;
            results.push(outcome_value(cmd, Some(&run), true, None));
            if run.blocked {
                let brief = stdout_brief(&run);
                let what = if event == "PreToolUse" {
                    format!("阻断了工具调用「{}」", tool.unwrap_or("?"))
                } else {
                    "要求继续执行".to_string()
                };
                notes.push(format!("Hook「{event}」{what}（exit 2）：{brief}"));
            } else if run.timed_out {
                notes.push(format!(
                    "Hook「{event}」命令 `{command}` 超时（{secs} 秒）被终止，本轮结果不可信"
                ));
            } else if run.exit_code != 0 {
                notes.push(format!(
                    "Hook「{event}」命令 `{command}` 执行失败（exit {}）：{}",
                    run.exit_code,
                    stdout_brief(&run)
                ));
            }
        }
    }

    if event == "Stop" && blocked {
        // Stop 的 exit 2 语义是「别停，继续跑」；引擎回复已结束，续跑做不到，必须如实讲
        notes.push("Stop Hook 要求继续执行（exit 2），但本轮回复已经收尾，当前执行流不支持续跑".to_string());
    }

    Ok(json!({
        "event": event,
        "results": results,
        "blocked": if is_blockable(event) { blocked } else { false },
        "notes": notes,
    }))
}

fn stdout_brief(run: &HookRun) -> String {
    let raw = if run.stdout.trim().is_empty() {
        run.stderr.as_str()
    } else {
        run.stdout.as_str()
    };
    let one = raw.trim().chars().take(200).collect::<String>();
    if one.is_empty() {
        "（脚本无输出）".to_string()
    } else {
        one
    }
}

/// 真实任务的事件输入（字段名对齐 contracts.ts HookInput）
pub fn real_input(event: &str, tool: Option<&str>, payload: &Value) -> Value {
    let mut v = json!({
        "session_id": payload.get("sessionId").and_then(|s| s.as_str()).unwrap_or(""),
        "cwd": payload.get("cwd").and_then(|s| s.as_str()).unwrap_or(""),
        "hook_event_name": event,
    });
    if let Some(t) = tool {
        v["tool_name"] = json!(t);
    }
    if let Some(ti) = payload.get("toolInput") {
        v["tool_input"] = ti.clone();
    }
    for key in ["source", "reason", "trigger", "type", "agent_type", "message", "tool_response"] {
        if let Some(x) = payload.get(key) {
            v[key] = x.clone();
        }
    }
    v
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

    // ── matcher 语义 ──────────────────────────────────────

    #[test]
    fn test_matcher_blank_and_star_match_everything() {
        for m in [None, Some(""), Some("   "), Some("*")] {
            assert!(matcher_matches(m, Some("Write")).unwrap(), "{m:?}");
        }
        // 非工具事件没有可比对字段时，matcher 不参与判定（而不是当成「不匹配」）
        assert!(matcher_matches(Some("Bash"), None).unwrap());
    }

    #[test]
    fn test_matcher_is_unanchored_regex() {
        assert!(matcher_matches(Some("Bash|Write"), Some("Write")).unwrap());
        assert!(!matcher_matches(Some("Bash|Write"), Some("Edit")).unwrap());
        assert!(matcher_matches(Some("^w"), Some("write_text_file")).unwrap());
        assert!(matcher_matches(Some("rite"), Some("write_text_file")).unwrap(), "不锚定");
    }

    #[test]
    fn test_matcher_invalid_regex_reports_instead_of_silently_skipping() {
        let err = matcher_matches(Some("Write("), Some("Write")).unwrap_err();
        assert!(err.starts_with("HOOK_INVALID_MATCHER"), "{err}");
    }

    #[test]
    fn test_match_target_picks_the_field_the_script_can_key_on() {
        assert_eq!(match_target("PreToolUse", Some("Write"), &json!({})).as_deref(), Some("Write"));
        assert_eq!(
            match_target("SessionStart", None, &json!({"source": "resume"})).as_deref(),
            Some("resume")
        );
        assert_eq!(
            match_target("SessionEnd", None, &json!({"reason": "cancelled"})).as_deref(),
            Some("cancelled")
        );
        assert_eq!(
            match_target("PreCompact", None, &json!({"trigger": "auto"})).as_deref(),
            Some("auto")
        );
        // 缺字段时按空串参与匹配（正则 ^$ 之类仍可判定），不能报「匹配不到」
        assert_eq!(match_target("SessionStart", None, &json!({})).as_deref(), Some(""));
    }

    // ── 放行台账 ──────────────────────────────────────────

    #[test]
    fn test_grant_is_idempotent_and_revoke_keeps_config() {
        let (store, dir) = temp_store("grants");
        let cfg = json!({"hooks": {"PreToolUse": [{"hooks": [
            {"type": "command", "command": "guard.sh", "timeout": 5}
        ]}]}});
        set(&store, &cfg, 10).unwrap();

        assert!(!is_granted(&store, "guard.sh").unwrap());
        grant(&store, "guard.sh", "PreToolUse", 111).unwrap();
        grant(&store, "  guard.sh  ", "PreToolUse", 222).unwrap(); // 前后空格算同一条
        assert!(is_granted(&store, "guard.sh").unwrap());

        let grants = list_grants(&store).unwrap();
        assert_eq!(grants.len(), 1, "重复放行只刷新时间，不产生多行");
        assert_eq!(grants[0]["grantedAtMs"], json!(222));
        assert_eq!(grants[0]["event"], "PreToolUse");

        revoke(&store, "guard.sh").unwrap();
        assert!(!is_granted(&store, "guard.sh").unwrap());
        // 撤权 ≠ 删配置：存量配置必须原样还在（数据保护规范）
        assert_eq!(get(&store).unwrap()["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "guard.sh");
        let rows: i64 = store
            .with_conn(|c| c.query_row("SELECT COUNT(*) FROM hook_configs", [], |r| r.get(0)).map_err(|e| e.to_string()))
            .unwrap();
        assert_eq!(rows, 1);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_grant_rejects_empty_command_and_unknown_event() {
        let (store, dir) = temp_store("grant-invalid");
        assert!(grant(&store, "  ", "Stop", 1).unwrap_err().starts_with("HOOK_INVALID_COMMAND"));
        assert!(grant(&store, "x", "Nope", 1).unwrap_err().starts_with("HOOK_INVALID_EVENT"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_configured_commands_dedupes_across_events() {
        let cfg = json!({"hooks": {
            "PreToolUse": [{"hooks": [{"command": "a.sh"}, {"command": " b.sh "}]}],
            "Stop": [{"hooks": [{"command": "a.sh"}, {"command": "  "}]}],
        }});
        assert_eq!(configured_commands(&cfg), vec!["a.sh", "b.sh"]);
        assert_eq!(configured_commands(&json!({})), Vec::<String>::new());
    }

    // ── 执行面概览 ────────────────────────────────────────

    #[test]
    fn test_runtime_summary_separates_configured_from_executable() {
        let (store, dir) = temp_store("summary");
        let cfg = json!({"hooks": {
            "PreToolUse": [{"hooks": [{"command": "guard.sh"}]}],
            "Notification": [{"hooks": [{"command": "notify.sh"}]}],
            "Stop": [],
        }});
        set(&store, &cfg, 1).unwrap();
        grant(&store, "guard.sh", "PreToolUse", 2).unwrap();

        let s = runtime_summary(&store).unwrap();
        assert_eq!(s["configured"], json!(2));
        assert_eq!(s["ungranted"], json!(["notify.sh"]));
        assert!(s["mountedEvents"].as_array().unwrap().contains(&json!("PreToolUse")));
        assert!(s["firedEvents"].as_array().unwrap().contains(&json!("UserPromptSubmit")));
        assert!(s["firedEvents"].as_array().unwrap().contains(&json!("SubagentStart")));
        // 配了但引擎没有触发点的事件要单独列出来，页面才能逐条说明「不会执行」
        assert_eq!(s["mountedUnfired"], json!(["Notification"]));
        let _ = std::fs::remove_dir_all(&dir);
    }

    // ── 真实触发 ──────────────────────────────────────────

    /// notes[] 拼成一段文本再断言：失败时要能看到全部说明，而不是只看第一条
    fn notes_of(v: &Value) -> String {
        v["notes"]
            .as_array()
            .cloned()
            .unwrap_or_default()
            .iter()
            .filter_map(|n| n.as_str())
            .collect::<Vec<_>>()
            .join(" | ")
    }

    /// 落到本用例自己的临时目录（每条命令都是唯一路径，落盘断言不会被上一轮残留蒙混）
    fn marker_in(dir: &std::path::Path) -> String {
        dir.join("fired.txt").to_string_lossy().to_string()
    }

    fn write_cmd(path: &str) -> String {
        // 不加引号：cmd 的转义规则会把 Rust 传的 \" 变成字面量，重定向路径反而失效
        if cfg!(windows) {
            format!("echo fired> {path}")
        } else {
            format!("echo fired > '{path}'")
        }
    }

    #[test]
    fn test_fire_without_config_is_silent_noop() {
        let (store, dir) = temp_store("fire-empty");
        let out = fire(&store, "PreToolUse", Some("Write"), &json!({}), 1).unwrap();
        assert_eq!(out["results"], json!([]));
        assert_eq!(out["blocked"], json!(false));
        assert_eq!(out["notes"], json!([]));
        assert!(fire(&store, "NotAnEvent", None, &json!({}), 1).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_fire_does_not_execute_ungranted_command() {
        let (store, dir) = temp_store("fire-ungranted");
        let marker = marker_in(&dir);
        set(&store, &json!({"hooks": {"Stop": [{"hooks": [
            {"command": write_cmd(&marker), "timeout": 20}
        ]}]}}), 1).unwrap();

        let out = fire(&store, "Stop", None, &json!({}), 5).unwrap();
        assert_eq!(out["results"][0]["executed"], json!(false));
        assert_eq!(out["results"][0]["granted"], json!(false));
        assert_eq!(out["results"][0]["reason"], "未放行");
        assert!(notes_of(&out).contains("尚未放行"), "{}", out["notes"]);
        assert!(!std::path::Path::new(&marker).exists(), "未放行绝不能真的拉起脚本");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_fire_runs_granted_command_once_and_counts_it() {
        let (store, dir) = temp_store("fire-granted");
        let cmd = write_cmd(&marker_in(&dir));
        set(&store, &json!({"hooks": {"PreToolUse": [{"matcher": "Write", "hooks": [
            {"command": &cmd, "timeout": 20}
        ]}]}}), 1).unwrap();
        grant(&store, &cmd, "PreToolUse", 2).unwrap();

        let out = fire(&store, "PreToolUse", Some("Write"), &json!({"toolInput": {"p": 1}}), 7).unwrap();
        assert_eq!(out["results"][0]["executed"], json!(true));
        assert_eq!(out["results"][0]["exitCode"], json!(0), "{}", out["results"]);
        assert_eq!(out["blocked"], json!(false));
        assert_eq!(out["notes"], json!([]), "成功就该一句话都不多说");
        let marker = marker_in(&dir);
        assert!(
            std::fs::read_to_string(&marker).unwrap_or_default().contains("fired"),
            "脚本必须真的被执行并落盘：{}", out["results"]
        );

        fire(&store, "PreToolUse", Some("Write"), &json!({}), 9).unwrap();
        let grants = list_grants(&store).unwrap();
        assert_eq!(grants[0]["runCount"], json!(2), "放行台账要记真实执行次数");
        assert_eq!(grants[0]["lastRunAtMs"], json!(9));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_fire_blocks_only_blockable_events_and_says_so() {
        let (store, dir) = temp_store("fire-block");
        let exit2 = if cfg!(windows) { "exit /b 2" } else { "exit 2" };
        set(&store, &json!({"hooks": {
            "PreToolUse": [{"hooks": [{"command": exit2}]}],
            "SessionStart": [{"hooks": [{"command": exit2}]}],
            "Stop": [{"hooks": [{"command": exit2}]}],
        }}), 1).unwrap();
        grant(&store, exit2, "PreToolUse", 2).unwrap();

        let pre = fire(&store, "PreToolUse", Some("write_text_file"), &json!({}), 3).unwrap();
        assert_eq!(pre["blocked"], json!(true));
        assert!(notes_of(&pre).contains("阻断了工具调用「write_text_file」"), "{}", pre["notes"]);

        let start = fire(&store, "SessionStart", None, &json!({"source": "desktop"}), 3).unwrap();
        assert_eq!(start["blocked"], json!(false), "SessionStart 没有阻塞语义");

        let stop = fire(&store, "Stop", None, &json!({}), 3).unwrap();
        assert_eq!(stop["blocked"], json!(true));
        assert!(
            notes_of(&stop).contains("不支持续跑"),
            "做不到就必须明说：{}", stop["notes"]
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_fire_skips_nonmatching_group_without_noise() {
        let (store, dir) = temp_store("fire-matcher");
        set(&store, &json!({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"command": "echo should-not-run"}
        ]}]}}), 1).unwrap();
        let out = fire(&store, "PreToolUse", Some("write_text_file"), &json!({}), 1).unwrap();
        assert_eq!(out["results"], json!([]));
        assert_eq!(out["notes"], json!([]));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_fire_surfaces_bad_matcher_instead_of_pretending_it_ran() {
        let (store, dir) = temp_store("fire-bad-matcher");
        set(&store, &json!({"hooks": {"PreToolUse": [{"matcher": "Write(", "hooks": [
            {"command": "echo x"}
        ]}]}}), 1).unwrap();
        let out = fire(&store, "PreToolUse", Some("Write"), &json!({}), 1).unwrap();
        assert!(notes_of(&out).contains("matcher"), "{}", out["notes"]);
        assert_eq!(out["results"], json!([]));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_fire_reports_timeout_and_nonzero_exit() {
        let (store, dir) = temp_store("fire-fail");
        let slow = if cfg!(windows) { "ping -n 20 127.0.0.1 >nul & exit /b 0" } else { "sleep 20" };
        let boom = if cfg!(windows) { "echo 出错啦 & exit /b 3" } else { "echo 出错啦; exit 3" };
        set(&store, &json!({"hooks": {"PostToolUse": [
            {"hooks": [{"command": slow, "timeout": 1}]},
            {"hooks": [{"command": boom, "timeout": 20}]}
        ]}}), 1).unwrap();
        grant(&store, slow, "PostToolUse", 2).unwrap();
        grant(&store, boom, "PostToolUse", 2).unwrap();

        let out = fire(&store, "PostToolUse", Some("Write"), &json!({}), 3).unwrap();
        assert!(out["results"][0]["timedOut"].as_bool().unwrap(), "{}", out["results"]);
        assert!(notes_of(&out).contains("超时"), "{}", out["notes"]);
        assert_eq!(out["results"][1]["exitCode"], json!(3));
        assert!(notes_of(&out).contains("执行失败"), "{}", out["notes"]);
        assert_eq!(out["blocked"], json!(false), "PostToolUse 不是可阻塞事件");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_real_input_shapes_the_contract_fields_scripts_expect() {
        let v = real_input(
            "PreToolUse",
            Some("write_text_file"),
            &json!({
                "sessionId": "task-1", "cwd": "d:/work",
                "toolInput": {"file_path": "a.md"}, "toolUseId": "call-1",
                "tool_response": "写入成功", "message": "写报表"
            }),
        );
        assert_eq!(v["session_id"], "task-1");
        assert_eq!(v["cwd"], "d:/work");
        assert_eq!(v["hook_event_name"], "PreToolUse");
        assert_eq!(v["tool_name"], "write_text_file");
        assert_eq!(v["tool_input"]["file_path"], "a.md");
        assert_eq!(v["tool_response"], "写入成功");
        assert_eq!(v["message"], "写报表");
        assert!(v.get("toolUseId").is_none(), "驼峰键不该漏进脚本输入");

        let light = real_input("SessionStart", None, &json!({"source": "desktop"}));
        assert_eq!(light["source"], "desktop");
        assert!(light.get("tool_name").is_none(), "非工具事件不该有空工具名");
    }

    fn which(bin: &str) -> Option<std::path::PathBuf> {
        std::env::var("PATH")
            .ok()?
            .split(if cfg!(windows) { ';' } else { ':' })
            .map(|p| std::path::Path::new(p).join(if cfg!(windows) { format!("{bin}.exe") } else { bin.to_string() }))
            .find(|p| p.exists())
    }
}
