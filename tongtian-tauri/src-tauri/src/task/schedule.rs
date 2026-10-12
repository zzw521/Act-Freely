//! 定时任务（ScheduleView 真实化）：任务定义 + 下次执行时间 + 执行记录
//!
//! 时间口径与用量台账一致：一律 UTC（`time_of_day` 为 UTC HH:MM）。
//! 无 chrono 依赖，日期换算用 Howard Hinnant civil_from_days / days_from_civil，
//! 与 `security::audit::iso8601_utc` 同一套算法（便于交叉印证）。
//!
//! 频率语义：
//! - hourly ：每小时的第 MM 分（HH 忽略）
//! - daily  ：每天 HH:MM
//! - weekly ：每周 `weekday`（1=周一…7=周日）的 HH:MM
//! - monthly：每月 1 日 HH:MM（无「月内第几日」锚点，取自然月首日）

use crate::store::Store;
use serde::{Deserialize, Serialize};

pub const FREQ_HOURLY: &str = "hourly";
pub const FREQ_DAILY: &str = "daily";
pub const FREQ_WEEKLY: &str = "weekly";
pub const FREQ_MONTHLY: &str = "monthly";

/// 执行记录状态
pub const RUN_RUNNING: &str = "running";
pub const RUN_SUCCEEDED: &str = "succeeded";
pub const RUN_FAILED: &str = "failed";
pub const RUN_BLOCKED: &str = "blocked";

/// 调度扫描周期（秒）：到点任务最多延迟一个周期被触发
pub const SCAN_INTERVAL_SECS: u64 = 30;

/// 定时任务定义
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ScheduleRow {
    pub schedule_id: String,
    pub name: String,
    pub description: String,
    pub freq: String,
    pub time_of_day: String,
    pub weekday: Option<i64>,
    pub enabled: bool,
    pub workspace_id: String,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
    pub next_run_at_ms: Option<i64>,
    pub last_run_at_ms: Option<i64>,
    pub last_status: Option<String>,
    pub last_task_id: Option<String>,
}

/// 单次执行记录
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ScheduleRunRow {
    pub run_id: String,
    pub schedule_id: String,
    pub task_id: Option<String>,
    pub started_at_ms: i64,
    pub finished_at_ms: Option<i64>,
    pub status: String,
    pub summary: Option<String>,
}

/// 新建入参（命令层已做规范化前的原始值）
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct NewSchedule {
    pub name: String,
    pub description: Option<String>,
    pub freq: String,
    pub time_of_day: String,
    pub weekday: Option<i64>,
    pub workspace_id: Option<String>,
}

// ── 规范化（错误码与前端 error-messages 对齐） ──────────────────

/// 频率规范化：接受英文枚举与界面中文标签（每天/每周/每月/每小时）
pub fn normalize_freq(v: &str) -> Result<String, String> {
    let t = v.trim();
    let t = match t {
        "每小时" => FREQ_HOURLY,
        "每天" | "每日" => FREQ_DAILY,
        "每周" | "每星期" => FREQ_WEEKLY,
        "每月" => FREQ_MONTHLY,
        other => other,
    };
    let lower = t.to_ascii_lowercase();
    match lower.as_str() {
        FREQ_HOURLY | FREQ_DAILY | FREQ_WEEKLY | FREQ_MONTHLY => Ok(lower),
        _ => Err(format!("SCHEDULE_INVALID_FREQ: {v}")),
    }
}

/// 时刻规范化：HH:MM（24 小时制，00:00–23:59）
pub fn normalize_time(v: &str) -> Result<String, String> {
    let (h, m) = parse_hhmm(v).ok_or_else(|| format!("SCHEDULE_INVALID_TIME: {v}"))?;
    Ok(format!("{h:02}:{m:02}"))
}

/// 星期规范化：1=周一 … 7=周日（仅 weekly 必填，其余频率忽略）
pub fn normalize_weekday(freq: &str, weekday: Option<i64>) -> Result<Option<i64>, String> {
    if freq != FREQ_WEEKLY {
        return Ok(None);
    }
    match weekday {
        Some(w) if (1..=7).contains(&w) => Ok(Some(w)),
        Some(w) => Err(format!("SCHEDULE_INVALID_WEEKDAY: {w}")),
        None => Err("SCHEDULE_INVALID_WEEKDAY: 每周任务需指定星期".into()),
    }
}

/// 名称规范化：去空白，空名拒绝
pub fn normalize_name(v: &str) -> Result<String, String> {
    let t = v.trim();
    if t.is_empty() {
        return Err("SCHEDULE_INVALID_NAME: 任务名称不能为空".into());
    }
    Ok(t.chars().take(60).collect())
}

fn parse_hhmm(v: &str) -> Option<(i64, i64)> {
    let t = v.trim();
    let (hs, ms) = t.split_once(':')?;
    let h: i64 = hs.trim().parse().ok()?;
    let m: i64 = ms.trim().parse().ok()?;
    if !(0..=23).contains(&h) || !(0..=59).contains(&m) {
        return None;
    }
    Some((h, m))
}

// ── 下次执行时间（纯函数，可单测） ─────────────────────────────

/// 计算 `after_ms` 之后的下一次执行时间（严格大于 after_ms；UTC）
pub fn next_run_at_ms(freq: &str, time_of_day: &str, weekday: Option<i64>, after_ms: i64) -> i64 {
    let (h, m) = parse_hhmm(time_of_day).unwrap_or((0, 0));
    let secs = after_ms.div_euclid(1000);
    let days = secs.div_euclid(86_400);
    let tod = secs.rem_euclid(86_400);
    let day_start = days * 86_400;
    let minute_offset = h * 3600 + m * 60;

    let target = match freq {
        FREQ_HOURLY => {
            let mut t = day_start + tod.div_euclid(3600) * 3600 + m * 60;
            while t <= secs {
                t += 3600;
            }
            t
        }
        FREQ_WEEKLY => {
            let wd = weekday.unwrap_or(1).clamp(1, 7);
            // 1970-01-01 为周四，1=周一 口径下偏移 +3 天
            let today_wd = (days + 3).rem_euclid(7) + 1;
            let delta = (wd - today_wd).rem_euclid(7);
            let mut t = day_start + delta * 86_400 + minute_offset;
            if t <= secs {
                t += 7 * 86_400;
            }
            t
        }
        FREQ_MONTHLY => {
            let (y, mo, _) = civil_from_days(days);
            let (ny, nmo) = if mo == 12 { (y + 1, 1) } else { (y, mo + 1) };
            let first = days_from_civil(ny, nmo, 1) * 86_400 + minute_offset;
            if first > secs {
                first
            } else {
                let (y2, m2) = if nmo == 12 { (ny + 1, 1) } else { (ny, nmo + 1) };
                days_from_civil(y2, m2, 1) * 86_400 + minute_offset
            }
        }
        // daily 与未知频率：未知频率已被 normalize_freq 拦截，此处按天兜底
        _ => {
            let mut t = day_start + minute_offset;
            if t <= secs {
                t += 86_400;
            }
            t
        }
    };
    target * 1000
}

/// 到点且启用的任务（`next_run_at_ms` 已过）
pub fn due(rows: &[ScheduleRow], now_ms: i64) -> Vec<ScheduleRow> {
    rows.iter()
        .filter(|r| r.enabled)
        .filter(|r| r.next_run_at_ms.map(|t| t <= now_ms).unwrap_or(false))
        .cloned()
        .collect()
}

// ── 持久化 ─────────────────────────────────────────────────

fn map_row(r: &rusqlite::Row<'_>) -> rusqlite::Result<ScheduleRow> {
    Ok(ScheduleRow {
        schedule_id: r.get(0)?,
        name: r.get(1)?,
        description: r.get(2)?,
        freq: r.get(3)?,
        time_of_day: r.get(4)?,
        weekday: r.get::<_, Option<i64>>(5)?,
        enabled: r.get::<_, i64>(6)? != 0,
        workspace_id: r.get(7)?,
        created_at_ms: r.get(8)?,
        updated_at_ms: r.get(9)?,
        next_run_at_ms: r.get::<_, Option<i64>>(10)?,
        last_run_at_ms: r.get::<_, Option<i64>>(11)?,
        last_status: r.get::<_, Option<String>>(12)?,
        last_task_id: r.get::<_, Option<String>>(13)?,
    })
}

const SELECT_COLS: &str = "schedule_id, name, description, freq, time_of_day, weekday, enabled, \
                           workspace_id, created_at_ms, updated_at_ms, next_run_at_ms, \
                           last_run_at_ms, last_status, last_task_id";

/// 全部定时任务（按下次执行时间升序，无下次执行者排后）
pub fn list(store: &Store) -> Result<Vec<ScheduleRow>, String> {
    store.with_conn(|c| {
        let sql = format!(
            "SELECT {SELECT_COLS} FROM scheduled_tasks \
             ORDER BY (next_run_at_ms IS NULL), next_run_at_ms ASC, created_at_ms DESC"
        );
        let mut stmt = c.prepare(&sql).map_err(|e| format!("SCHEDULE_QUERY_FAILED: {e}"))?;
        let rows = stmt
            .query_map([], |r| map_row(r))
            .map_err(|e| format!("SCHEDULE_QUERY_FAILED: {e}"))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("SCHEDULE_QUERY_FAILED: {e}"))
    })
}

pub fn get(store: &Store, schedule_id: &str) -> Result<Option<ScheduleRow>, String> {
    store.with_conn(|c| {
        let sql = format!("SELECT {SELECT_COLS} FROM scheduled_tasks WHERE schedule_id = ?1");
        let mut stmt = c.prepare(&sql).map_err(|e| format!("SCHEDULE_QUERY_FAILED: {e}"))?;
        let mut rows = stmt
            .query_map([schedule_id], |r| map_row(r))
            .map_err(|e| format!("SCHEDULE_QUERY_FAILED: {e}"))?;
        match rows.next() {
            Some(Ok(row)) => Ok(Some(row)),
            Some(Err(e)) => Err(format!("SCHEDULE_QUERY_FAILED: {e}")),
            None => Ok(None),
        }
    })
}

/// 新建定时任务（入参先规范化；创建即计算首次执行时间）
pub fn create(store: &Store, req: &NewSchedule, now_ms: i64) -> Result<ScheduleRow, String> {
    let name = normalize_name(&req.name)?;
    let freq = normalize_freq(&req.freq)?;
    let time_of_day = normalize_time(&req.time_of_day)?;
    let weekday = normalize_weekday(&freq, req.weekday)?;
    let description = req.description.clone().unwrap_or_default();
    let workspace_id = req
        .workspace_id
        .clone()
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| "default".into());
    let row = ScheduleRow {
        schedule_id: format!("sched-{now_ms}-{}", &uuid::Uuid::new_v4().simple().to_string()[..8]),
        name,
        description,
        freq: freq.clone(),
        time_of_day: time_of_day.clone(),
        weekday,
        enabled: true,
        workspace_id,
        created_at_ms: now_ms,
        updated_at_ms: now_ms,
        next_run_at_ms: Some(next_run_at_ms(&freq, &time_of_day, weekday, now_ms)),
        last_run_at_ms: None,
        last_status: None,
        last_task_id: None,
    };
    store.with_conn(|c| {
        c.execute(
            "INSERT INTO scheduled_tasks (schedule_id, name, description, freq, time_of_day, \
             weekday, enabled, workspace_id, created_at_ms, updated_at_ms, next_run_at_ms) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
            rusqlite::params![
                row.schedule_id,
                row.name,
                row.description,
                row.freq,
                row.time_of_day,
                row.weekday,
                row.enabled as i64,
                row.workspace_id,
                row.created_at_ms,
                row.updated_at_ms,
                row.next_run_at_ms,
            ],
        )
        .map_err(|e| format!("SCHEDULE_SAVE_FAILED: {e}"))?;
        Ok(())
    })?;
    Ok(row)
}

/// 编辑入参（缺省字段沿用现有配置，只改用户真正改动的部分）
#[derive(Debug, Clone, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct UpdateSchedule {
    pub schedule_id: String,
    pub name: Option<String>,
    pub description: Option<String>,
    pub freq: Option<String>,
    pub time_of_day: Option<String>,
    pub weekday: Option<i64>,
    pub workspace_id: Option<String>,
}

/// 启用/禁用：禁用清空下次执行时间，启用按当前时刻重算
pub fn set_enabled(
    store: &Store,
    schedule_id: &str,
    enabled: bool,
    now_ms: i64,
) -> Result<ScheduleRow, String> {
    let cur = get(store, schedule_id)?.ok_or_else(|| format!("SCHEDULE_NOT_FOUND: {schedule_id}"))?;
    let next = if enabled {
        Some(next_run_at_ms(&cur.freq, &cur.time_of_day, cur.weekday, now_ms))
    } else {
        None
    };
    store.with_conn(|c| {
        c.execute(
            "UPDATE scheduled_tasks SET enabled = ?2, next_run_at_ms = ?3, updated_at_ms = ?4 \
             WHERE schedule_id = ?1",
            rusqlite::params![schedule_id, enabled as i64, next, now_ms],
        )
        .map_err(|e| format!("SCHEDULE_SAVE_FAILED: {e}"))?;
        Ok(())
    })?;
    let mut row = cur;
    row.enabled = enabled;
    row.next_run_at_ms = next;
    row.updated_at_ms = now_ms;
    Ok(row)
}

/// 编辑定时任务：入参先规范化，按当前时刻重算下次执行时间（禁用的任务保持不排期）
pub fn update(store: &Store, req: &UpdateSchedule, now_ms: i64) -> Result<ScheduleRow, String> {
    let cur = get(store, &req.schedule_id)?
        .ok_or_else(|| format!("SCHEDULE_NOT_FOUND: {}", req.schedule_id))?;

    let name = match req.name.as_deref() {
        Some(v) => normalize_name(v)?,
        None => cur.name.clone(),
    };
    let description = match req.description.as_deref() {
        Some(v) => v.trim().to_string(),
        None => cur.description.clone(),
    };
    let freq = match req.freq.as_deref() {
        Some(v) => normalize_freq(v)?,
        None => cur.freq.clone(),
    };
    let time_of_day = match req.time_of_day.as_deref() {
        Some(v) => normalize_time(v)?,
        None => cur.time_of_day.clone(),
    };
    // 星期：改频率即重算（weekly 必填，其余频率清空）；未改频率沿用原值
    let weekday = if req.freq.is_some() || req.weekday.is_some() {
        normalize_weekday(&freq, req.weekday.or(cur.weekday))?
    } else {
        cur.weekday
    };
    let workspace_id = match req.workspace_id.as_deref() {
        Some(v) if !v.trim().is_empty() => v.trim().to_string(),
        Some(_) => "default".to_string(),
        None => cur.workspace_id.clone(),
    };

    let next = if cur.enabled {
        Some(next_run_at_ms(&freq, &time_of_day, weekday, now_ms))
    } else {
        None
    };

    store.with_conn(|c| {
        c.execute(
            "UPDATE scheduled_tasks SET name = ?2, description = ?3, freq = ?4, time_of_day = ?5, \
             weekday = ?6, workspace_id = ?7, next_run_at_ms = ?8, updated_at_ms = ?9 \
             WHERE schedule_id = ?1",
            rusqlite::params![
                req.schedule_id,
                name,
                description,
                freq,
                time_of_day,
                weekday,
                workspace_id,
                next,
                now_ms,
            ],
        )
        .map_err(|e| format!("SCHEDULE_SAVE_FAILED: {e}"))?;
        Ok(())
    })?;

    let mut row = cur;
    row.name = name;
    row.description = description;
    row.freq = freq;
    row.time_of_day = time_of_day;
    row.weekday = weekday;
    row.workspace_id = workspace_id;
    row.next_run_at_ms = next;
    row.updated_at_ms = now_ms;
    Ok(row)
}

/// 删除定时任务（连同其执行记录）
pub fn remove(store: &Store, schedule_id: &str) -> Result<bool, String> {
    let existed = get(store, schedule_id)?.is_some();
    if !existed {
        return Ok(false);
    }
    store.with_conn(|c| {
        c.execute(
            "DELETE FROM scheduled_tasks WHERE schedule_id = ?1",
            [schedule_id],
        )
        .map_err(|e| format!("SCHEDULE_SAVE_FAILED: {e}"))?;
        c.execute(
            "DELETE FROM schedule_runs WHERE schedule_id = ?1",
            [schedule_id],
        )
        .map_err(|e| format!("SCHEDULE_SAVE_FAILED: {e}"))?;
        Ok(())
    })?;
    Ok(true)
}

/// 触发前推进下次执行时间：避免同一到点任务被扫描循环重复触发
pub fn advance_next(store: &Store, schedule_id: &str, now_ms: i64) -> Result<(), String> {
    let cur = get(store, schedule_id)?.ok_or_else(|| format!("SCHEDULE_NOT_FOUND: {schedule_id}"))?;
    let next = next_run_at_ms(&cur.freq, &cur.time_of_day, cur.weekday, now_ms);
    store.with_conn(|c| {
        c.execute(
            "UPDATE scheduled_tasks SET next_run_at_ms = ?2, updated_at_ms = ?3 WHERE schedule_id = ?1",
            rusqlite::params![schedule_id, next, now_ms],
        )
        .map_err(|e| format!("SCHEDULE_SAVE_FAILED: {e}"))?;
        Ok(())
    })
}

/// 记一条执行开始（返回 run_id）
pub fn start_run(
    store: &Store,
    schedule_id: &str,
    task_id: Option<&str>,
    now_ms: i64,
) -> Result<String, String> {
    let run_id = format!("run-{now_ms}-{}", &uuid::Uuid::new_v4().simple().to_string()[..8]);
    store.with_conn(|c| {
        c.execute(
            "INSERT INTO schedule_runs (run_id, schedule_id, task_id, started_at_ms, status) \
             VALUES (?1, ?2, ?3, ?4, ?5)",
            rusqlite::params![run_id, schedule_id, task_id, now_ms, RUN_RUNNING],
        )
        .map_err(|e| format!("SCHEDULE_SAVE_FAILED: {e}"))?;
        Ok(())
    })?;
    Ok(run_id)
}

/// 记一条执行结束，并回写任务的上次执行状态与下次执行时间
pub fn finish_run(
    store: &Store,
    run_id: &str,
    schedule_id: &str,
    task_id: Option<&str>,
    status: &str,
    summary: &str,
    now_ms: i64,
) -> Result<(), String> {
    let next = get(store, schedule_id)?
        .map(|r| next_run_at_ms(&r.freq, &r.time_of_day, r.weekday, now_ms));
    store.with_conn(|c| {
        c.execute(
            "UPDATE schedule_runs SET status = ?2, summary = ?3, finished_at_ms = ?4, task_id = COALESCE(?5, task_id) \
             WHERE run_id = ?1",
            rusqlite::params![run_id, status, summary, now_ms, task_id],
        )
        .map_err(|e| format!("SCHEDULE_SAVE_FAILED: {e}"))?;
        c.execute(
            "UPDATE scheduled_tasks SET last_run_at_ms = ?2, last_status = ?3, \
             last_task_id = COALESCE(?4, last_task_id), next_run_at_ms = ?5, updated_at_ms = ?2 \
             WHERE schedule_id = ?1",
            rusqlite::params![schedule_id, now_ms, status, task_id, next],
        )
        .map_err(|e| format!("SCHEDULE_SAVE_FAILED: {e}"))?;
        Ok(())
    })
}

/// 执行记录（可按任务过滤；按开始时间倒序）
pub fn list_runs(
    store: &Store,
    schedule_id: Option<&str>,
    limit: usize,
) -> Result<Vec<ScheduleRunRow>, String> {
    let limit = if limit == 0 { 100 } else { limit.min(500) } as i64;
    store.with_conn(|c| {
        let map = |r: &rusqlite::Row<'_>| -> rusqlite::Result<ScheduleRunRow> {
            Ok(ScheduleRunRow {
                run_id: r.get(0)?,
                schedule_id: r.get(1)?,
                task_id: r.get(2)?,
                started_at_ms: r.get(3)?,
                finished_at_ms: r.get(4)?,
                status: r.get(5)?,
                summary: r.get(6)?,
            })
        };
        let cols = "run_id, schedule_id, task_id, started_at_ms, finished_at_ms, status, summary";
        let rows = match schedule_id {
            Some(id) => {
                let sql = format!(
                    "SELECT {cols} FROM schedule_runs WHERE schedule_id = ?1 \
                     ORDER BY started_at_ms DESC LIMIT ?2"
                );
                let mut stmt = c.prepare(&sql).map_err(|e| format!("SCHEDULE_QUERY_FAILED: {e}"))?;
                let it = stmt
                    .query_map(rusqlite::params![id, limit], |r| map(r))
                    .map_err(|e| format!("SCHEDULE_QUERY_FAILED: {e}"))?;
                it.collect::<Result<Vec<_>, _>>()
            }
            None => {
                let sql =
                    format!("SELECT {cols} FROM schedule_runs ORDER BY started_at_ms DESC LIMIT ?1");
                let mut stmt = c.prepare(&sql).map_err(|e| format!("SCHEDULE_QUERY_FAILED: {e}"))?;
                let it = stmt
                    .query_map(rusqlite::params![limit], |r| map(r))
                    .map_err(|e| format!("SCHEDULE_QUERY_FAILED: {e}"))?;
                it.collect::<Result<Vec<_>, _>>()
            }
        };
        rows.map_err(|e| format!("SCHEDULE_QUERY_FAILED: {e}"))
    })
}

// ── 日期换算（Howard Hinnant 算法，与 audit::iso8601_utc 同源） ──

fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

fn days_from_civil(y: i64, m: u32, d: u32) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = (y - era * 400) as u64;
    let mp = if m > 2 { m - 3 } else { m + 9 } as u64;
    let doy = (153 * mp + 2) / 5 + d as u64 - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe as i64 - 719_468
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn temp_store(name: &str) -> (Store, PathBuf) {
        let dir = std::env::temp_dir().join(format!("tt-sched-{name}-{}", crate::bridge::event_store::now_ms()));
        std::fs::create_dir_all(&dir).unwrap();
        let store = Store::open(dir.join("tongtian.db")).unwrap();
        (store, dir)
    }

    /// 2026-03-05T00:00:00Z（周四）
    fn base_ms() -> i64 {
        1_772_668_800_000
    }

    fn new_req(freq: &str, time: &str, weekday: Option<i64>) -> NewSchedule {
        NewSchedule {
            name: "每日舆情".into(),
            description: Some("汇总品牌舆情".into()),
            freq: freq.into(),
            time_of_day: time.into(),
            weekday,
            workspace_id: None,
        }
    }

    #[test]
    fn test_normalize_freq_accepts_cn_labels_and_enums() {
        assert_eq!(normalize_freq("每天").unwrap(), FREQ_DAILY);
        assert_eq!(normalize_freq("每周").unwrap(), FREQ_WEEKLY);
        assert_eq!(normalize_freq("每月").unwrap(), FREQ_MONTHLY);
        assert_eq!(normalize_freq("每小时").unwrap(), FREQ_HOURLY);
        assert_eq!(normalize_freq("DAILY").unwrap(), FREQ_DAILY);
        let err = normalize_freq("每分钟").unwrap_err();
        assert!(err.starts_with("SCHEDULE_INVALID_FREQ:"), "{err}");
    }

    #[test]
    fn test_normalize_time_bounds_and_padding() {
        assert_eq!(normalize_time("9:5").unwrap(), "09:05");
        assert_eq!(normalize_time(" 23:59 ").unwrap(), "23:59");
        assert!(normalize_time("24:00").unwrap_err().starts_with("SCHEDULE_INVALID_TIME:"));
        assert!(normalize_time("12:60").unwrap_err().starts_with("SCHEDULE_INVALID_TIME:"));
        assert!(normalize_time("abc").unwrap_err().starts_with("SCHEDULE_INVALID_TIME:"));
    }

    #[test]
    fn test_normalize_weekday_only_for_weekly() {
        assert_eq!(normalize_weekday(FREQ_WEEKLY, Some(3)).unwrap(), Some(3));
        assert_eq!(normalize_weekday(FREQ_DAILY, Some(3)).unwrap(), None);
        assert!(normalize_weekday(FREQ_WEEKLY, Some(0)).unwrap_err().starts_with("SCHEDULE_INVALID_WEEKDAY:"));
        assert!(normalize_weekday(FREQ_WEEKLY, None).unwrap_err().starts_with("SCHEDULE_INVALID_WEEKDAY:"));
    }

    #[test]
    fn test_next_daily_same_day_when_future() {
        // 基准 00:00，目标 08:00 当天
        let t = next_run_at_ms(FREQ_DAILY, "08:00", None, base_ms());
        assert_eq!(t, base_ms() + 8 * 3600 * 1000);
    }

    #[test]
    fn test_next_daily_rolls_to_tomorrow_when_passed() {
        let noon = base_ms() + 12 * 3600 * 1000;
        let t = next_run_at_ms(FREQ_DAILY, "08:00", None, noon);
        assert_eq!(t, base_ms() + 32 * 3600 * 1000);
    }

    #[test]
    fn test_next_daily_is_strictly_after_now() {
        // 恰好等于当前分钟：必须推到次日（避免刚触发又立即命中）
        let t = next_run_at_ms(FREQ_DAILY, "08:00", None, base_ms() + 8 * 3600 * 1000);
        assert_eq!(t, base_ms() + 32 * 3600 * 1000);
    }

    #[test]
    fn test_next_weekly_picks_target_weekday() {
        // 基准周四（2026-03-05），目标周一 09:00 → 2026-03-09（4 天后）
        let t = next_run_at_ms(FREQ_WEEKLY, "09:00", Some(1), base_ms());
        assert_eq!(t, base_ms() + 4 * 86_400_000 + 9 * 3600 * 1000);
    }

    #[test]
    fn test_next_weekly_same_weekday_rolls_seven_days() {
        // 目标周四＝当天，但 09:00 已过（基准 12:00）→ 顺延 7 天
        let noon = base_ms() + 12 * 3600 * 1000;
        let t = next_run_at_ms(FREQ_WEEKLY, "09:00", Some(4), noon);
        assert_eq!(t, noon + 6 * 86_400_000 + 21 * 3600 * 1000);
    }

    #[test]
    fn test_next_monthly_first_day_of_next_month() {
        // 2026-03-05 → 2026-04-01 07:30
        let t = next_run_at_ms(FREQ_MONTHLY, "07:30", None, base_ms());
        let expect_days = days_from_civil(2026, 4, 1) * 86_400 + 7 * 3600 + 30 * 60;
        assert_eq!(t, expect_days * 1000);
    }

    #[test]
    fn test_next_monthly_rolls_year_boundary() {
        // 2026-12-20 → 2027-01-01
        let dec = days_from_civil(2026, 12, 20) * 86_400 * 1000;
        let t = next_run_at_ms(FREQ_MONTHLY, "00:00", None, dec);
        assert_eq!(t, days_from_civil(2027, 1, 1) * 86_400 * 1000);
    }

    #[test]
    fn test_next_hourly_uses_minute_field() {
        // 00:20 基准，目标 :30 → 当天 00:30
        let t = next_run_at_ms(FREQ_HOURLY, "09:30", None, base_ms() + 20 * 60 * 1000);
        assert_eq!(t, base_ms() + 30 * 60 * 1000);
    }

    #[test]
    fn test_next_hourly_rolls_next_hour() {
        let t = next_run_at_ms(FREQ_HOURLY, "09:30", None, base_ms() + 45 * 60 * 1000);
        assert_eq!(t, base_ms() + 90 * 60 * 1000);
    }

    #[test]
    fn test_civil_days_roundtrip() {
        for (y, m, d) in [(1970, 1, 1), (2026, 3, 5), (2026, 12, 31), (2027, 1, 1)] {
            let days = days_from_civil(y, m, d);
            assert_eq!(civil_from_days(days), (y, m, d));
        }
    }

    #[test]
    fn test_create_list_and_get() {
        let (store, dir) = temp_store("crud");
        let row = create(&store, &new_req("每天", "08:00", None), base_ms()).unwrap();
        assert_eq!(row.freq, FREQ_DAILY);
        assert_eq!(row.time_of_day, "08:00");
        assert!(row.enabled);
        assert_eq!(row.next_run_at_ms, Some(base_ms() + 8 * 3600 * 1000));

        let rows = list(&store).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].schedule_id, row.schedule_id);
        assert_eq!(get(&store, &row.schedule_id).unwrap().unwrap(), row);
        assert!(get(&store, "sched-missing").unwrap().is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_create_rejects_bad_input() {
        let (store, dir) = temp_store("bad");
        assert!(create(&store, &new_req("每天", "08:00", None).with_name(" "), base_ms())
            .unwrap_err()
            .starts_with("SCHEDULE_INVALID_NAME:"));
        assert!(create(&store, &new_req("每分钟", "08:00", None), base_ms())
            .unwrap_err()
            .starts_with("SCHEDULE_INVALID_FREQ:"));
        assert!(create(&store, &new_req("每天", "8h", None), base_ms())
            .unwrap_err()
            .starts_with("SCHEDULE_INVALID_TIME:"));
        assert!(create(&store, &new_req("每周", "08:00", None), base_ms())
            .unwrap_err()
            .starts_with("SCHEDULE_INVALID_WEEKDAY:"));
        assert_eq!(list(&store).unwrap().len(), 0, "非法入参不得落库");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_set_enabled_clears_and_restores_next_run() {
        let (store, dir) = temp_store("toggle");
        let row = create(&store, &new_req("每天", "08:00", None), base_ms()).unwrap();
        let off = set_enabled(&store, &row.schedule_id, false, base_ms() + 1000).unwrap();
        assert!(!off.enabled);
        assert_eq!(off.next_run_at_ms, None);
        assert!(due(&list(&store).unwrap(), base_ms() + 86_400_000).is_empty());

        let on = set_enabled(&store, &row.schedule_id, true, base_ms() + 1000).unwrap();
        assert!(on.enabled);
        assert_eq!(on.next_run_at_ms, Some(base_ms() + 8 * 3600 * 1000));
        assert!(set_enabled(&store, "sched-none", true, base_ms())
            .unwrap_err()
            .starts_with("SCHEDULE_NOT_FOUND:"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_update_changes_only_given_fields_and_recomputes_next() {
        let (store, dir) = temp_store("upd-partial");
        let row = create(&store, &new_req("每天", "08:00", None), base_ms()).unwrap();

        let patched = update(
            &store,
            &UpdateSchedule {
                schedule_id: row.schedule_id.clone(),
                time_of_day: Some("20:00".into()),
                ..Default::default()
            },
            base_ms() + 60_000,
        )
        .unwrap();

        assert_eq!(patched.name, row.name, "未提供的名称保持原值");
        assert_eq!(patched.description, row.description);
        assert_eq!(patched.freq, FREQ_DAILY);
        assert_eq!(patched.time_of_day, "20:00");
        assert_eq!(patched.created_at_ms, row.created_at_ms);
        assert_eq!(patched.updated_at_ms, base_ms() + 60_000);
        // 仍在同一时刻之后重算：base_ms 为 00:00，下次执行当天 20:00
        assert_eq!(patched.next_run_at_ms, Some(base_ms() + 20 * 3600 * 1000));
        assert_eq!(get(&store, &row.schedule_id).unwrap().unwrap().time_of_day, "20:00");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_update_switches_freq_and_weekday() {
        let (store, dir) = temp_store("upd-freq");
        let weekly = create(&store, &new_req("每周", "08:00", Some(3)), base_ms()).unwrap();
        assert_eq!(weekly.weekday, Some(3));

        // 每周 → 每天：星期不再有意义，落库为 NULL
        let daily = update(
            &store,
            &UpdateSchedule {
                schedule_id: weekly.schedule_id.clone(),
                freq: Some("每天".into()),
                ..Default::default()
            },
            base_ms(),
        )
        .unwrap();
        assert_eq!(daily.freq, FREQ_DAILY);
        assert_eq!(daily.weekday, None);

        // 每天 → 每周：缺星期即拒绝
        let err = update(
            &store,
            &UpdateSchedule {
                schedule_id: weekly.schedule_id.clone(),
                freq: Some("每周".into()),
                ..Default::default()
            },
            base_ms(),
        )
        .unwrap_err();
        assert!(err.starts_with("SCHEDULE_INVALID_WEEKDAY:"), "{err}");
        assert_eq!(
            get(&store, &weekly.schedule_id).unwrap().unwrap().freq,
            FREQ_DAILY,
            "被拒的编辑不改动存量任务"
        );

        // 补上星期后成功，并沿用原时刻
        let back = update(
            &store,
            &UpdateSchedule {
                schedule_id: weekly.schedule_id.clone(),
                freq: Some("每周".into()),
                weekday: Some(6),
                ..Default::default()
            },
            base_ms(),
        )
        .unwrap();
        assert_eq!(back.freq, FREQ_WEEKLY);
        assert_eq!(back.weekday, Some(6));
        assert_eq!(back.time_of_day, "08:00");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_update_keeps_disabled_task_unscheduled() {
        let (store, dir) = temp_store("upd-disabled");
        let row = create(&store, &new_req("每天", "08:00", None), base_ms()).unwrap();
        set_enabled(&store, &row.schedule_id, false, base_ms()).unwrap();

        let edited = update(
            &store,
            &UpdateSchedule {
                schedule_id: row.schedule_id.clone(),
                time_of_day: Some("09:30".into()),
                ..Default::default()
            },
            base_ms() + 1000,
        )
        .unwrap();
        assert!(!edited.enabled);
        assert_eq!(edited.next_run_at_ms, None, "禁用任务编辑后仍不排期");

        // 重新启用时按新时刻排期
        let on = set_enabled(&store, &row.schedule_id, true, base_ms()).unwrap();
        assert_eq!(on.next_run_at_ms, Some(base_ms() + 9 * 3600 * 1000 + 30 * 60 * 1000));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_update_workspace_and_description() {
        let (store, dir) = temp_store("upd-ws");
        let row = create(&store, &new_req("每天", "08:00", None), base_ms()).unwrap();
        assert_eq!(row.workspace_id, "default");

        let edited = update(
            &store,
            &UpdateSchedule {
                schedule_id: row.schedule_id.clone(),
                workspace_id: Some(" ops ".into()),
                description: Some("".into()),
                ..Default::default()
            },
            base_ms(),
        )
        .unwrap();
        assert_eq!(edited.workspace_id, "ops");
        assert_eq!(edited.description, "");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_update_rejects_bad_input_and_missing_task() {
        let (store, dir) = temp_store("upd-bad");
        let row = create(&store, &new_req("每天", "08:00", None), base_ms()).unwrap();

        assert!(update(
            &store,
            &UpdateSchedule {
                schedule_id: row.schedule_id.clone(),
                name: Some("   ".into()),
                ..Default::default()
            },
            base_ms(),
        )
        .unwrap_err()
        .starts_with("SCHEDULE_INVALID_NAME:"));

        assert!(update(
            &store,
            &UpdateSchedule {
                schedule_id: row.schedule_id.clone(),
                time_of_day: Some("25:00".into()),
                ..Default::default()
            },
            base_ms(),
        )
        .unwrap_err()
        .starts_with("SCHEDULE_INVALID_TIME:"));

        assert!(update(
            &store,
            &UpdateSchedule {
                schedule_id: "sched-missing".into(),
                name: Some("改名".into()),
                ..Default::default()
            },
            base_ms(),
        )
        .unwrap_err()
        .starts_with("SCHEDULE_NOT_FOUND:"));

        // 空标识：拒绝，不做全表匹配
        assert!(update(
            &store,
            &UpdateSchedule {
                schedule_id: "  ".into(),
                name: Some("改名".into()),
                ..Default::default()
            },
            base_ms(),
        )
        .unwrap_err()
        .starts_with("SCHEDULE_NOT_FOUND:"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_remove_deletes_task_and_runs() {
        let (store, dir) = temp_store("remove");
        let row = create(&store, &new_req("每天", "08:00", None), base_ms()).unwrap();
        let run_id = start_run(&store, &row.schedule_id, Some("task-1"), base_ms()).unwrap();
        finish_run(&store, &run_id, &row.schedule_id, Some("task-1"), RUN_SUCCEEDED, "完成", base_ms() + 10).unwrap();
        assert_eq!(list_runs(&store, None, 10).unwrap().len(), 1);

        assert!(remove(&store, &row.schedule_id).unwrap());
        assert_eq!(list(&store).unwrap().len(), 0);
        assert_eq!(list_runs(&store, None, 10).unwrap().len(), 0);
        assert!(!remove(&store, &row.schedule_id).unwrap(), "重复删除返回 false");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_due_selects_only_enabled_and_reached() {
        let (store, dir) = temp_store("due");
        let a = create(&store, &new_req("每天", "08:00", None), base_ms()).unwrap();
        let b = create(&store, &new_req("每天", "20:00", None), base_ms()).unwrap();
        set_enabled(&store, &b.schedule_id, false, base_ms()).unwrap();

        let rows = list(&store).unwrap();
        assert!(due(&rows, base_ms()).is_empty(), "未到点不触发");
        let hit = due(&rows, base_ms() + 8 * 3600 * 1000);
        assert_eq!(hit.len(), 1);
        assert_eq!(hit[0].schedule_id, a.schedule_id);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_advance_next_prevents_double_trigger() {
        let (store, dir) = temp_store("advance");
        let row = create(&store, &new_req("每天", "08:00", None), base_ms()).unwrap();
        let at = base_ms() + 8 * 3600 * 1000;
        advance_next(&store, &row.schedule_id, at).unwrap();
        let after = get(&store, &row.schedule_id).unwrap().unwrap();
        assert_eq!(after.next_run_at_ms, Some(base_ms() + 32 * 3600 * 1000));
        assert!(due(&list(&store).unwrap(), at).is_empty(), "推进后不再命中");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_start_and_finish_run_writes_back_schedule() {
        let (store, dir) = temp_store("run");
        let row = create(&store, &new_req("每天", "08:00", None), base_ms()).unwrap();
        let at = base_ms() + 8 * 3600 * 1000;
        let run_id = start_run(&store, &row.schedule_id, None, at).unwrap();
        let running = list_runs(&store, Some(&row.schedule_id), 10).unwrap();
        assert_eq!(running[0].status, RUN_RUNNING);
        assert_eq!(running[0].finished_at_ms, None);

        finish_run(&store, &run_id, &row.schedule_id, Some("task-9"), RUN_SUCCEEDED, "产出 3 步", at + 5).unwrap();
        let done = list_runs(&store, Some(&row.schedule_id), 10).unwrap();
        assert_eq!(done[0].status, RUN_SUCCEEDED);
        assert_eq!(done[0].task_id.as_deref(), Some("task-9"));
        assert_eq!(done[0].finished_at_ms, Some(at + 5));
        assert_eq!(done[0].summary.as_deref(), Some("产出 3 步"));

        let sched = get(&store, &row.schedule_id).unwrap().unwrap();
        assert_eq!(sched.last_status.as_deref(), Some(RUN_SUCCEEDED));
        assert_eq!(sched.last_task_id.as_deref(), Some("task-9"));
        assert_eq!(sched.last_run_at_ms, Some(at + 5));
        assert_eq!(sched.next_run_at_ms, Some(base_ms() + 32 * 3600 * 1000));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_list_runs_limit_and_order() {
        let (store, dir) = temp_store("runs-order");
        let row = create(&store, &new_req("每天", "08:00", None), base_ms()).unwrap();
        for i in 0..3 {
            let at = base_ms() + i * 1000;
            let rid = start_run(&store, &row.schedule_id, None, at).unwrap();
            finish_run(&store, &rid, &row.schedule_id, None, RUN_FAILED, "失败", at + 1).unwrap();
        }
        let rows = list_runs(&store, None, 2).unwrap();
        assert_eq!(rows.len(), 2);
        assert!(rows[0].started_at_ms > rows[1].started_at_ms, "按开始时间倒序");
        assert_eq!(list_runs(&store, None, 0).unwrap().len(), 3, "limit=0 取缺省 100");
        let _ = std::fs::remove_dir_all(&dir);
    }

    impl NewSchedule {
        fn with_name(mut self, name: &str) -> Self {
            self.name = name.into();
            self
        }
    }
}