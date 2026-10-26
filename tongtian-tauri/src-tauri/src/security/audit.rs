//! 全链路审计日志（PRD §4.7.4）
//!
//! - 留痕范围：模型调用、文件操作、配置变更、登录态变更、任务提交/取消
//! - 落库前统一脱敏（`classify::redact` + `redact_paths`）：Key / PII / 路径用户名不入库
//! - 查询支持动作、严重度、时间窗过滤；导出 CSV/JSON 至运行目录 `exports/`
//! - 纯追加写入，本模块不提供删除接口（存量数据保护，开发规范）

use std::path::Path;

use serde::{Deserialize, Serialize};

use crate::security::classify::{redact, redact_paths};
use crate::store::{settings, Store};

// ── 动作码（与前端 AuditLog 组件的标签映射一致） ─────────────────

pub const ACTION_MODEL_CALL: &str = "model_call";
pub const ACTION_FILE_READ: &str = "file_read";
pub const ACTION_FILE_WRITE: &str = "file_write";
pub const ACTION_ROUTE_SWITCH: &str = "route_switch";
pub const ACTION_CONFIG_CHANGE: &str = "config_change";
pub const ACTION_USER_LOGIN: &str = "user_login";
pub const ACTION_USER_LOGOUT: &str = "user_logout";
pub const ACTION_TASK_SUBMIT: &str = "task_submit";
pub const ACTION_TASK_CANCEL: &str = "task_cancel";
pub const ACTION_SCHEDULE_RUN: &str = "schedule_run";
pub const ACTION_SANDBOX_VIOLATION: &str = "sandbox_violation";
/// 数据分级检测留痕（合规 spec：数据流向可追溯；resource 记命中等级）
pub const ACTION_DATA_CHECK: &str = "data_check";
/// 提交前合规闸门留痕（resource 记判定结果，details 记端点与等级；拒绝时升级为 warning）
pub const ACTION_COMPLIANCE_CHECK: &str = "compliance_check";
/// 用户 Hook 在真实任务里拉起进程留痕（resource 记事件名，details 记命令与退出码）
pub const ACTION_HOOK_RUN: &str = "hook_run";
/// 受控命令执行（P1：仅白名单命令、无 shell；resource 记命令，details 记退出码）
pub const ACTION_EXEC_RUN: &str = "exec_run";
/// 受控联网获取（P1：仅白名单公网主机；resource 记 URL，details 记状态与大小）
pub const ACTION_NETWORK_FETCH: &str = "network_fetch";
/// 电脑操控截屏（P1/P2：resource 记来源，details 记尺寸与大小；不含画面内容）
pub const ACTION_COMPUTER_CAPTURE: &str = "computer_capture";
/// 电脑操控操作（P1/P2：resource 记来源，details 记动作与坐标；不记输入正文）
pub const ACTION_COMPUTER_ACTION: &str = "computer_action";
/// 受控浏览器会话（启动/结束；resource 记浏览器，details 记配置目录与端口）
pub const ACTION_BROWSER_SESSION: &str = "browser_session";
/// 受控浏览器页面操作（导航/快照/点击/输入/滚动/截屏；不记页面正文与输入正文）
pub const ACTION_BROWSER_ACTION: &str = "browser_action";
/// 在线市场安装（resource 记条目标识，details 记来源主机与版本；不记下载地址查询串）
pub const ACTION_MARKET_INSTALL: &str = "market_install";
/// MCP 连接器 OAuth 授权/撤销（resource 记连接器标识，details 不记令牌与授权码）
pub const ACTION_CONNECTOR_AUTH: &str = "connector_auth";
/// 逐工具审批（放行/拒绝；resource 记工具名，details 记入参摘要与结果，不含正文与密钥）
pub const ACTION_TOOL_APPROVAL: &str = "tool_approval";
/// 终端 AI+：会话开关 / 输入 / AI 建议（输入正文不落库，只记来源与字符数）
pub const ACTION_TERMINAL_SESSION: &str = "terminal_session";
pub const ACTION_TERMINAL_INPUT: &str = "terminal_input";
pub const ACTION_TERMINAL_AI: &str = "terminal_ai";
/// ACP 委派：配置变更写桌面审计，协议级明细由引擎写 {home}/acp/audit.jsonl
pub const ACTION_ACP_CONFIG: &str = "acp_config";
/// 远程桌面：只记会话开始/结束/到期与帧数，画面内容不入库
pub const ACTION_REMOTE_SESSION: &str = "remote_session";

pub const SEV_INFO: &str = "info";
pub const SEV_WARNING: &str = "warning";
pub const SEV_ERROR: &str = "error";

/// 默认返回上限（防止一次拉取撑爆前端表格）
pub const DEFAULT_LIMIT: usize = 500;
/// 请求上限封顶（防止全表扫描）
pub const MAX_LIMIT: usize = 5000;

/// 审计条目（与前端 AuditLogEntry 契约一致）
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AuditEntry {
    pub id: String,
    /// ISO-8601 UTC（前端 `new Date(timestamp)` 可直接解析）
    pub timestamp: String,
    pub action: String,
    pub severity: String,
    pub user: Option<String>,
    pub resource: Option<String>,
    pub details: Option<String>,
    pub read: bool,
}

/// 查询过滤条件（前端传 `{type, severity, dateRange, limit}`）
#[derive(Debug, Default, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AuditFilters {
    /// 动作码；`type` 为前端侧别名
    #[serde(alias = "type")]
    pub action: Option<String>,
    pub severity: Option<String>,
    /// 7d / 30d / 90d / all
    pub date_range: Option<String>,
    pub limit: Option<usize>,
}

// ── 时间工具（自实现 ISO-8601，避免引入 chrono：随包体积 + 离线构建约束） ──

/// epoch 毫秒 → ISO-8601 UTC，形如 `2026-09-21T00:00:00.000Z`
pub fn iso8601_utc(ms: i64) -> String {
    let secs = ms.div_euclid(1000);
    let millis = ms.rem_euclid(1000);
    let days = secs.div_euclid(86_400);
    let tod = secs.rem_euclid(86_400);
    let (y, m, d) = civil_from_days(days);
    let (hh, mm, ss) = (tod / 3600, (tod % 3600) / 60, tod % 60);
    format!("{y:04}-{m:02}-{d:02}T{hh:02}:{mm:02}:{ss:02}.{millis:03}Z")
}

/// Howard Hinnant `civil_from_days`：1970-01-01 起天数 → 公历年月日
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = (if z >= 0 { z } else { z - 146_096 }) / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = (if mp < 10 { mp + 3 } else { mp - 9 }) as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

/// 当前登录用户昵称（未登录 / profile 非法时返回 None）
fn current_user(store: &Store) -> Option<String> {
    let raw = settings::get(store, crate::auth::service::PROFILE_KEY)
        .ok()
        .flatten()?;
    let v: serde_json::Value = serde_json::from_str(&raw).ok()?;
    v.get("nickname")
        .and_then(|s| s.as_str())
        .or_else(|| v.get("uid").and_then(|s| s.as_str()))
        .map(|s| s.to_string())
}

// ── 写入 ────────────────────────────────────────────────────────

/// 追加一条审计记录，返回记录 id。
/// 脱敏在写入前统一完成，调用方无需自行处理敏感内容。
pub fn record(
    store: &Store,
    action: &str,
    severity: &str,
    resource: &str,
    details: &str,
) -> Result<String, String> {
    let id = uuid::Uuid::new_v4().to_string();
    let user = current_user(store);
    let resource = redact_paths(&redact(resource));
    let details = redact_paths(&redact(details));
    let now = crate::bridge::event_store::now_ms() as i64;
    store.with_conn(|c| {
        c.execute(
            "INSERT INTO audit_logs (id, ts_ms, action, severity, user_name, resource, details, is_read) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 0)",
            rusqlite::params![id, now, action, severity, user, resource, details],
        )
        .map(|_| ())
        .map_err(|e| format!("AUDIT_WRITE_FAILED: {e}"))
    })?;
    Ok(id)
}

/// 尽力而为的留痕：审计写入失败不阻断主流程（调用方不需要处理错误）
pub fn record_quiet(store: &Store, action: &str, severity: &str, resource: &str, details: &str) {
    if let Err(e) = record(store, action, severity, resource, details) {
        eprintln!("[audit] 留痕失败（不阻断主流程）: {e}");
    }
}

// ── 查询 ────────────────────────────────────────────────────────

/// 时间窗 → 起始毫秒（`all` / 非法值 → 不过滤）
fn range_cutoff_ms(range: &str) -> Option<i64> {
    let days = match range {
        "7d" => 7,
        "30d" => 30,
        "90d" => 90,
        _ => return None,
    };
    Some(crate::bridge::event_store::now_ms() as i64 - days * 86_400_000)
}

/// 按过滤条件查询（按时间倒序，默认 500 条、封顶 5000 条）
pub fn query(store: &Store, filters: &AuditFilters) -> Result<Vec<AuditEntry>, String> {
    let mut sql = String::from(
        "SELECT id, ts_ms, action, severity, user_name, resource, details, is_read \
         FROM audit_logs WHERE 1 = 1",
    );
    let mut args: Vec<rusqlite::types::Value> = Vec::new();

    if let Some(a) = filters.action.as_deref().filter(|s| !s.is_empty() && *s != "all") {
        sql.push_str(" AND action = ?");
        args.push(rusqlite::types::Value::Text(a.to_string()));
    }
    if let Some(sv) = filters.severity.as_deref().filter(|s| !s.is_empty() && *s != "all") {
        sql.push_str(" AND severity = ?");
        args.push(rusqlite::types::Value::Text(sv.to_string()));
    }
    if let Some(cut) = filters.date_range.as_deref().and_then(range_cutoff_ms) {
        sql.push_str(" AND ts_ms >= ?");
        args.push(rusqlite::types::Value::Integer(cut));
    }
    sql.push_str(" ORDER BY ts_ms DESC, id DESC LIMIT ?");
    args.push(rusqlite::types::Value::Integer(
        filters.limit.unwrap_or(DEFAULT_LIMIT).min(MAX_LIMIT) as i64,
    ));

    store.with_conn(|c| {
        let mut stmt = c
            .prepare(&sql)
            .map_err(|e| format!("AUDIT_QUERY_FAILED: {e}"))?;
        let rows = stmt
            .query_map(rusqlite::params_from_iter(args.iter()), |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, String>(3)?,
                    r.get::<_, Option<String>>(4)?,
                    r.get::<_, Option<String>>(5)?,
                    r.get::<_, Option<String>>(6)?,
                    r.get::<_, i64>(7)?,
                ))
            })
            .map_err(|e| format!("AUDIT_QUERY_FAILED: {e}"))?;

        let mut out = Vec::new();
        for row in rows {
            let (id, ts, action, severity, user, resource, details, is_read) =
                row.map_err(|e| format!("AUDIT_QUERY_FAILED: {e}"))?;
            out.push(AuditEntry {
                id,
                timestamp: iso8601_utc(ts),
                action,
                severity,
                user,
                resource,
                details,
                read: is_read != 0,
            });
        }
        Ok(out)
    })
}

// ── 导出（PRD §4.7.4：支持筛选、导出、归档） ─────────────────────

/// 导出到运行目录 `exports/`，返回 (文件路径, 条数)
pub fn export(
    store: &Store,
    format: &str,
    filters: &AuditFilters,
) -> Result<(String, usize), String> {
    let dir = crate::store::db::runtime_home().join("exports");
    export_to(store, format, filters, &dir)
}

/// 导出到指定目录（测试可注入临时目录，避免环境变量竞争）
pub fn export_to(
    store: &Store,
    format: &str,
    filters: &AuditFilters,
    dir: &Path,
) -> Result<(String, usize), String> {
    let fmt = match format {
        "csv" => "csv",
        "json" => "json",
        other => return Err(format!("AUDIT_EXPORT_FORMAT_UNSUPPORTED: {other}")),
    };
    let entries = query(store, filters)?;
    std::fs::create_dir_all(dir).map_err(|e| format!("AUDIT_EXPORT_FAILED: {e}"))?;
    // 时间戳做文件名：只新建文件，不触碰任何存量文件
    let stamp = iso8601_utc(crate::bridge::event_store::now_ms() as i64)
        .replace(':', "-")
        .replace('.', "-");
    let path = dir.join(format!("audit-{stamp}.{fmt}"));
    let body = if fmt == "json" {
        serde_json::to_string_pretty(&entries)
            .map_err(|e| format!("AUDIT_EXPORT_FAILED: {e}"))?
    } else {
        to_csv(&entries)
    };
    std::fs::write(&path, body).map_err(|e| format!("AUDIT_EXPORT_FAILED: {e}"))?;
    Ok((path.to_string_lossy().to_string(), entries.len()))
}

/// RFC 4180 CSV（CRLF 行尾 + 单元格转义）
fn to_csv(entries: &[AuditEntry]) -> String {
    let mut out = String::from("id,timestamp,action,severity,user,resource,details,read\r\n");
    for e in entries {
        let cells = [
            e.id.clone(),
            e.timestamp.clone(),
            e.action.clone(),
            e.severity.clone(),
            e.user.clone().unwrap_or_default(),
            e.resource.clone().unwrap_or_default(),
            e.details.clone().unwrap_or_default(),
            if e.read { "true".to_string() } else { "false".to_string() },
        ];
        out.push_str(&cells.map(csv_cell).join(","));
        out.push_str("\r\n");
    }
    out
}

/// CSV 单元格转义：含逗号/引号/换行时整体加引号，内部引号翻倍
fn csv_cell(v: String) -> String {
    if v.contains(',') || v.contains('"') || v.contains('\n') || v.contains('\r') {
        format!("\"{}\"", v.replace('"', "\"\""))
    } else {
        v
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn temp_store(name: &str) -> (Store, PathBuf) {
        let dir = std::env::temp_dir().join(format!(
            "tt-audit-{name}-{}",
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let store = Store::open(dir.join("t.db")).unwrap();
        (store, dir)
    }

    #[test]
    fn test_iso8601_known_values() {
        assert_eq!(iso8601_utc(0), "1970-01-01T00:00:00.000Z");
        // 2026-09-21T00:00:00Z = 20717 天 × 86400s
        assert_eq!(iso8601_utc(20717 * 86_400_000), "2026-09-21T00:00:00.000Z");
        // 含毫秒与日内时间
        assert_eq!(
            iso8601_utc(20717 * 86_400_000 + 36_000_000 + 123),
            "2026-09-21T10:00:00.123Z"
        );
        // 闰年边界
        assert_eq!(iso8601_utc(1_709_164_800_000), "2024-02-29T00:00:00.000Z");
    }

    #[test]
    fn test_record_and_query_roundtrip() {
        let (store, dir) = temp_store("rt");
        let id = record(&store, ACTION_MODEL_CALL, SEV_INFO, "qwen-max", "调用成功").unwrap();
        let list = query(&store, &AuditFilters::default()).unwrap();
        assert_eq!(list.len(), 1);
        assert_eq!(list[0].id, id);
        assert_eq!(list[0].action, ACTION_MODEL_CALL);
        assert_eq!(list[0].severity, SEV_INFO);
        assert_eq!(list[0].resource.as_deref(), Some("qwen-max"));
        assert!(!list[0].read);
        assert!(list[0].timestamp.starts_with("20"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_query_empty_table() {
        let (store, dir) = temp_store("empty");
        assert!(query(&store, &AuditFilters::default()).unwrap().is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_record_redacts_secrets_and_pii() {
        let (store, dir) = temp_store("redact");
        record(
            &store,
            ACTION_MODEL_CALL,
            SEV_ERROR,
            "sk-abcdefghijklmn",
            r"失败于 C:\Users\alice\cfg.json 手机 13800138000",
        )
        .unwrap();
        let e = &query(&store, &AuditFilters::default()).unwrap()[0];
        let resource = e.resource.clone().unwrap();
        let details = e.details.clone().unwrap();
        assert!(!resource.contains("abcdefghijklmn"), "密钥未脱敏: {resource}");
        assert!(!details.contains("alice"), "路径用户名未脱敏: {details}");
        assert!(!details.contains("13800138000"), "手机号未脱敏: {details}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_query_filters() {
        let (store, dir) = temp_store("filters");
        record(&store, ACTION_MODEL_CALL, SEV_INFO, "m1", "").unwrap();
        record(&store, ACTION_FILE_WRITE, SEV_ERROR, "f1", "").unwrap();
        record(&store, ACTION_FILE_READ, SEV_WARNING, "f2", "").unwrap();

        let by_action = AuditFilters {
            action: Some(ACTION_FILE_WRITE.into()),
            ..Default::default()
        };
        let r = query(&store, &by_action).unwrap();
        assert_eq!(r.len(), 1);
        assert_eq!(r[0].action, ACTION_FILE_WRITE);

        let by_sev = AuditFilters {
            severity: Some(SEV_INFO.into()),
            ..Default::default()
        };
        assert_eq!(query(&store, &by_sev).unwrap().len(), 1);

        // all / 空串视为不过滤
        let no_filter = AuditFilters {
            action: Some("all".into()),
            severity: Some(String::new()),
            ..Default::default()
        };
        assert_eq!(query(&store, &no_filter).unwrap().len(), 3);

        // 时间窗过滤：未来 cutoff 不可能命中（用 7d 反向验证不会误伤当天记录）
        let recent = AuditFilters {
            date_range: Some("7d".into()),
            ..Default::default()
        };
        assert_eq!(query(&store, &recent).unwrap().len(), 3);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_query_limit_and_order() {
        let (store, dir) = temp_store("limit");
        for i in 0..5 {
            record(&store, ACTION_MODEL_CALL, SEV_INFO, &format!("m{i}"), "").unwrap();
        }
        let limited = AuditFilters {
            limit: Some(2),
            ..Default::default()
        };
        assert_eq!(query(&store, &limited).unwrap().len(), 2);
        // 倒序：最后写入的排在最前
        let all = query(&store, &AuditFilters::default()).unwrap();
        assert_eq!(all[0].resource.as_deref(), Some("m4"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_export_json_and_csv() {
        let (store, dir) = temp_store("export");
        record(&store, ACTION_MODEL_CALL, SEV_INFO, "qwen-max", "含逗号,与引号\"的详情").unwrap();
        let out_dir = dir.join("out");

        let (json_path, n) = export_to(&store, "json", &AuditFilters::default(), &out_dir).unwrap();
        assert_eq!(n, 1);
        let json = std::fs::read_to_string(&json_path).unwrap();
        assert!(json.contains(ACTION_MODEL_CALL));
        serde_json::from_str::<Vec<AuditEntry>>(&json).unwrap();

        let (csv_path, n2) = export_to(&store, "csv", &AuditFilters::default(), &out_dir).unwrap();
        assert_eq!(n2, 1);
        let csv = std::fs::read_to_string(&csv_path).unwrap();
        assert!(csv.starts_with("id,timestamp,action,severity,user,resource,details,read\r\n"));
        // 含逗号/引号的单元格被整体加引号且内部引号翻倍
        assert!(csv.contains("\"含逗号,与引号\"\"的详情\""));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_export_rejects_unknown_format() {
        let (store, dir) = temp_store("fmt");
        let err = export_to(&store, "xml", &AuditFilters::default(), &dir.join("out")).unwrap_err();
        assert!(err.starts_with("AUDIT_EXPORT_FORMAT_UNSUPPORTED"), "{err}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_export_empty_is_valid_file() {
        let (store, dir) = temp_store("export-empty");
        let (path, n) = export_to(&store, "csv", &AuditFilters::default(), &dir.join("out")).unwrap();
        assert_eq!(n, 0);
        let csv = std::fs::read_to_string(&path).unwrap();
        assert_eq!(csv.lines().count(), 1);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_record_quiet_swallows_failure() {
        // 关闭底层表模拟写失败：record_quiet 不应 panic
        let (store, dir) = temp_store("quiet");
        store
            .with_conn(|c| c.execute_batch("DROP TABLE audit_logs").map_err(|e| e.to_string()))
            .unwrap();
        record_quiet(&store, ACTION_MODEL_CALL, SEV_INFO, "m", "d");
        assert!(query(&store, &AuditFilters::default()).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_current_user_from_profile() {
        let (store, dir) = temp_store("user");
        assert_eq!(current_user(&store), None);
        settings::set(&store, crate::auth::service::PROFILE_KEY, "{\"nickname\":\"张先生\"}").unwrap();
        assert_eq!(current_user(&store).as_deref(), Some("张先生"));
        // profile 非法 JSON：不报错，视为未登录
        settings::set(&store, crate::auth::service::PROFILE_KEY, "not-json").unwrap();
        assert_eq!(current_user(&store), None);
        let _ = std::fs::remove_dir_all(&dir);
    }
}