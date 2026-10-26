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
use std::sync::OnceLock;

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
    /// 执行完成后推送的 IM 通道（空＝不推送）
    pub notify_channel: String,
    /// 推送目标会话（Slack 频道 ID / 钉钉会话 ID 等）
    pub notify_target: String,
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
    pub notify_channel: Option<String>,
    pub notify_target: Option<String>,
}

/// 自然语言解析结果：字段可直接回填新建表单，`note` 说明做了哪些默认假设
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ParsedSchedule {
    pub freq: String,
    pub time_of_day: String,
    pub weekday: Option<i64>,
    pub name: String,
    pub note: Option<String>,
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

/// 推送配置规范化：通道与目标要么都空（不推送），要么都填
pub fn normalize_notify(
    channel: Option<&str>,
    target: Option<&str>,
) -> Result<(String, String), String> {
    let ch = channel.unwrap_or("").trim().to_ascii_lowercase();
    let tg = target.unwrap_or("").trim().to_string();
    if ch.is_empty() && tg.is_empty() {
        return Ok((String::new(), String::new()));
    }
    if ch.is_empty() {
        return Err("SCHEDULE_INVALID_NOTIFY: 已填写推送目标，但未选择推送通道".into());
    }
    if tg.is_empty() {
        return Err("SCHEDULE_INVALID_NOTIFY: 已选择推送通道，但未填写目标会话".into());
    }
    if ch.len() > 32
        || !ch
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-')
    {
        return Err(format!("SCHEDULE_INVALID_NOTIFY: 推送通道不合法（{ch}）"));
    }
    if tg.chars().count() > 128 {
        return Err("SCHEDULE_INVALID_NOTIFY: 推送目标过长（上限 128 字）".into());
    }
    Ok((ch, tg))
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

// ── 自然语言解析（「每周一上午9点生成周报」→ 表单字段） ──────────

fn pattern(cell: &'static OnceLock<regex::Regex>, pat: &str) -> &'static regex::Regex {
    cell.get_or_init(|| regex::Regex::new(pat).expect("内置正则必须有效"))
}

fn re_interval_hours() -> &'static regex::Regex {
    static R: OnceLock<regex::Regex> = OnceLock::new();
    pattern(&R, r"(?:每|每隔)\s*[0-9一二三四五六七八九十两]+\s*(?:个)?\s*(?:小时|钟头)")
}

fn re_interval_minutes() -> &'static regex::Regex {
    static R: OnceLock<regex::Regex> = OnceLock::new();
    pattern(&R, r"(?:每|每隔)\s*[0-9一二三四五六七八九十两]+\s*(?:分钟|分)")
}

fn re_weekly() -> &'static regex::Regex {
    static R: OnceLock<regex::Regex> = OnceLock::new();
    pattern(&R, r"每(?:周|星期|礼拜)\s*([一二三四五六日天七1-7])")
}

fn re_hourly() -> &'static regex::Regex {
    static R: OnceLock<regex::Regex> = OnceLock::new();
    pattern(&R, r"每(?:个)?小时")
}

fn re_monthly_day() -> &'static regex::Regex {
    static R: OnceLock<regex::Regex> = OnceLock::new();
    pattern(&R, r"每月\s*([0-9]{1,2}|[一二三四五六七八九十]{1,3})\s*[号日]")
}

fn re_monthly() -> &'static regex::Regex {
    static R: OnceLock<regex::Regex> = OnceLock::new();
    pattern(&R, r"每月")
}

fn re_daily() -> &'static regex::Regex {
    static R: OnceLock<regex::Regex> = OnceLock::new();
    pattern(&R, r"每(?:天|日)|天天")
}

fn re_period() -> &'static regex::Regex {
    static R: OnceLock<regex::Regex> = OnceLock::new();
    pattern(&R, r"(凌晨|清晨|早晨|早上|上午|中午|下午|傍晚|晚上|夜里|夜间|晚间)")
}

fn re_colon_time() -> &'static regex::Regex {
    static R: OnceLock<regex::Regex> = OnceLock::new();
    pattern(&R, r"(\d{1,2})\s*[:：]\s*(\d{1,2})")
}

fn re_point_time() -> &'static regex::Regex {
    static R: OnceLock<regex::Regex> = OnceLock::new();
    pattern(
        &R,
        r"([0-9]{1,2}|[零一二三四五六七八九十两]{1,3})\s*[点时](?:\s*(半)|([0-9]{1,2})\s*分?)?",
    )
}

fn re_minute_only() -> &'static regex::Regex {
    static R: OnceLock<regex::Regex> = OnceLock::new();
    pattern(&R, r"([0-9]{1,2})\s*分(?:钟)?")
}

/// 中文数字（1–99）：零/一/二/两…十、十一、二十三
fn cn_number(v: &str) -> Option<i64> {
    let t = v.trim();
    if let Ok(n) = t.parse::<i64>() {
        return Some(n);
    }
    let digit = |c: char| -> Option<i64> {
        Some(match c {
            '零' => 0,
            '一' => 1,
            '二' | '两' => 2,
            '三' => 3,
            '四' => 4,
            '五' => 5,
            '六' => 6,
            '七' => 7,
            '八' => 8,
            '九' => 9,
            _ => return None,
        })
    };
    if let Some((tens, ones)) = t.split_once('十') {
        let hi = if tens.is_empty() {
            1
        } else {
            tens.chars().next().and_then(digit)?
        };
        let lo = if ones.is_empty() {
            0
        } else {
            ones.chars().next().and_then(digit)?
        };
        return Some(hi * 10 + lo);
    }
    let mut chars = t.chars();
    let n = digit(chars.next()?)?;
    if chars.next().is_some() {
        return None;
    }
    Some(n)
}

fn weekday_of(v: &str) -> Option<i64> {
    Some(match v {
        "一" | "1" => 1,
        "二" | "2" => 2,
        "三" | "3" => 3,
        "四" | "4" => 4,
        "五" | "5" => 5,
        "六" | "6" => 6,
        "日" | "天" | "七" | "7" => 7,
        _ => return None,
    })
}

/// 时段词 → 24 小时制偏移。返回 None 表示该组合不是合法时刻。
fn apply_period(h: i64, period: Option<&str>) -> Option<i64> {
    let h = match period {
        Some("凌晨") => {
            if h == 12 {
                0
            } else {
                h
            }
        }
        Some("清晨") | Some("早晨") | Some("早上") | Some("上午") => {
            if h == 12 {
                0
            } else {
                h
            }
        }
        Some("中午") => {
            if h < 11 {
                h + 12
            } else {
                h
            }
        }
        Some("下午") | Some("傍晚") => {
            if h < 12 {
                h + 12
            } else {
                h
            }
        }
        Some("晚上") | Some("夜里") | Some("夜间") | Some("晚间") => {
            if h == 12 {
                0
            } else if h < 12 {
                h + 12
            } else {
                h
            }
        }
        _ => h,
    };
    (0..=23).contains(&h).then_some(h)
}

struct TimeHit {
    hour: i64,
    minute: i64,
    token: String,
}

fn find_time(raw: &str, freq: &str) -> Option<TimeHit> {
    let period = re_period().find(raw).map(|m| m.as_str().to_string());
    if let Some(c) = re_colon_time().captures(raw) {
        let h: i64 = c.get(1)?.as_str().parse().ok()?;
        let m: i64 = c.get(2)?.as_str().parse().ok()?;
        // 已经是 24 小时制（>12）时忽略时段词："晚上 20:00" 就是 20:00
        let hour = if h > 12 {
            h
        } else {
            apply_period(h, period.as_deref())?
        };
        return Some(TimeHit {
            hour,
            minute: m,
            token: c.get(0)?.as_str().to_string(),
        });
    }
    if let Some(c) = re_point_time().captures(raw) {
        let h = cn_number(c.get(1)?.as_str())?;
        let minute = if c.get(2).is_some() {
            30
        } else {
            c.get(3)
                .map(|m| m.as_str().parse::<i64>().ok())
                .unwrap_or(Some(0))?
        };
        let hour = if h > 12 {
            h
        } else {
            apply_period(h, period.as_deref())?
        };
        return Some(TimeHit {
            hour,
            minute,
            token: c.get(0)?.as_str().to_string(),
        });
    }
    // 每小时任务允许只写分钟："每小时的第 30 分"
    if freq == FREQ_HOURLY {
        if let Some(c) = re_minute_only().captures(raw) {
            let minute: i64 = c.get(1)?.as_str().parse().ok()?;
            return Some(TimeHit {
                hour: 0,
                minute,
                token: c.get(0)?.as_str().to_string(),
            });
        }
    }
    None
}

fn clean_schedule_name(raw: &str, tokens: &[String]) -> String {
    let mut s = raw.to_string();
    for t in tokens {
        if !t.is_empty() {
            s = s.replacen(t.as_str(), "", 1);
        }
    }
    const LEADING: &[&str] = &[
        "请帮我", "麻烦", "帮我", "请", "定时", "提醒我", "提醒", "我要", "我想", "需要",
        "的第", "第", "的",
    ];
    const TRAILING: &[&str] = &["提醒我", "提醒", "执行", "任务"];
    loop {
        let before = s.clone();
        s = s.trim().to_string();
        for f in LEADING {
            if let Some(rest) = s.strip_prefix(f) {
                s = rest.trim_start().to_string();
            }
        }
        for f in TRAILING {
            if let Some(head) = s.strip_suffix(f) {
                s = head.trim_end().to_string();
            }
        }
        if s == before {
            break;
        }
    }
    let trimmed = s.trim_matches(|c: char| {
        c.is_whitespace() || matches!(c, '，' | ',' | '。' | '；' | ';' | '：' | ':' | '！' | '!' | '？' | '?' | '、' | '-' | '—')
    });
    if trimmed.is_empty() {
        "定时任务".to_string()
    } else {
        trimmed.chars().take(60).collect()
    }
}

/// 把一句话解析成定时任务字段。只接受本调度器真实支持的四种频率；
/// 不支持的间隔/月内日期明确报错，不猜一个近似值。
pub fn parse_natural(text: &str) -> Result<ParsedSchedule, String> {
    let raw = text.trim();
    if raw.is_empty() {
        return Err(
            "SCHEDULE_PARSE_EMPTY: 请用一句话描述执行时间，例如「每周一上午 9 点生成周报」".into(),
        );
    }
    if re_interval_hours().is_match(raw) || re_interval_minutes().is_match(raw) {
        return Err(
            "SCHEDULE_UNSUPPORTED_FREQ: 暂不支持按「每 N 小时 / 每 N 分钟」间隔调度，请改用每小时、每天、每周或每月".into(),
        );
    }

    let mut tokens: Vec<String> = Vec::new();
    let mut freq: Option<String> = None;
    let mut weekday: Option<i64> = None;

    if let Some(c) = re_weekly().captures(raw) {
        let wd = weekday_of(c.get(1).map(|m| m.as_str()).unwrap_or(""))
            .ok_or_else(|| format!("SCHEDULE_PARSE_FAILED: 无法识别星期（{raw}）"))?;
        freq = Some(FREQ_WEEKLY.into());
        weekday = Some(wd);
        tokens.push(c.get(0).map(|m| m.as_str()).unwrap_or("").to_string());
    } else if let Some(c) = re_monthly_day().captures(raw) {
        let day = cn_number(c.get(1).map(|m| m.as_str()).unwrap_or(""))
            .ok_or_else(|| format!("SCHEDULE_PARSE_FAILED: 无法识别日期（{raw}）"))?;
        if day != 1 {
            return Err(format!(
                "SCHEDULE_UNSUPPORTED_DAY: 当前调度只支持每月 1 日（收到 {day} 号），请改用每周或每天"
            ));
        }
        freq = Some(FREQ_MONTHLY.into());
        tokens.push(c.get(0).map(|m| m.as_str()).unwrap_or("").to_string());
    } else if let Some(m) = re_hourly().find(raw) {
        freq = Some(FREQ_HOURLY.into());
        tokens.push(m.as_str().to_string());
    } else if let Some(m) = re_monthly().find(raw) {
        freq = Some(FREQ_MONTHLY.into());
        tokens.push(m.as_str().to_string());
    } else if let Some(m) = re_daily().find(raw) {
        freq = Some(FREQ_DAILY.into());
        tokens.push(m.as_str().to_string());
    }

    let mut note = None;
    let freq = match freq {
        Some(f) => f,
        None => {
            note = Some("未写频率，已按每天处理".to_string());
            FREQ_DAILY.to_string()
        }
    };

    let hit = find_time(raw, &freq).ok_or_else(|| {
        if freq == FREQ_HOURLY {
            "SCHEDULE_PARSE_NO_TIME: 没识别到分钟，请补充「每小时的第 30 分」这类时间".to_string()
        } else {
            "SCHEDULE_PARSE_NO_TIME: 没识别到执行时刻，请补充「上午 9 点」这类时间".to_string()
        }
    })?;
    if !(0..=23).contains(&hit.hour) || !(0..=59).contains(&hit.minute) {
        return Err(format!(
            "SCHEDULE_PARSE_TIME: 无法识别有效时刻（{}:{}）",
            hit.hour, hit.minute
        ));
    }
    tokens.push(hit.token.clone());
    if let Some(p) = re_period().find(raw) {
        if !hit.token.contains(p.as_str()) {
            tokens.push(p.as_str().to_string());
        }
    }

    let name = clean_schedule_name(raw, &tokens);
    Ok(ParsedSchedule {
        freq,
        time_of_day: format!("{:02}:{:02}", hit.hour, hit.minute),
        weekday,
        name,
        note,
    })
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
        notify_channel: r.get(14)?,
        notify_target: r.get(15)?,
    })
}

const SELECT_COLS: &str = "schedule_id, name, description, freq, time_of_day, weekday, enabled, \
                           workspace_id, created_at_ms, updated_at_ms, next_run_at_ms, \
                           last_run_at_ms, last_status, last_task_id, notify_channel, notify_target";

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
    let (notify_channel, notify_target) = normalize_notify(
        req.notify_channel.as_deref(),
        req.notify_target.as_deref(),
    )?;
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
        notify_channel,
        notify_target,
    };
    store.with_conn(|c| {
        c.execute(
            "INSERT INTO scheduled_tasks (schedule_id, name, description, freq, time_of_day, \
             weekday, enabled, workspace_id, created_at_ms, updated_at_ms, next_run_at_ms, \
             notify_channel, notify_target) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)",
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
                row.notify_channel,
                row.notify_target,
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
    pub notify_channel: Option<String>,
    pub notify_target: Option<String>,
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
    // 推送配置：都不传＝保持；通道传空串＝关闭推送（同时清目标）；
    // 只改目标沿用原通道；切换通道但没给目标时不继承旧会话标识。
    let (notify_channel, notify_target) =
        if req.notify_channel.is_none() && req.notify_target.is_none() {
            (cur.notify_channel.clone(), cur.notify_target.clone())
        } else {
            let ch = req
                .notify_channel
                .as_deref()
                .map(|v| v.trim().to_ascii_lowercase())
                .unwrap_or_else(|| cur.notify_channel.clone());
            if ch.is_empty() {
                (String::new(), String::new())
            } else {
                let tg = req
                    .notify_target
                    .as_deref()
                    .map(|v| v.trim().to_string())
                    .unwrap_or_else(|| {
                        if ch == cur.notify_channel {
                            cur.notify_target.clone()
                        } else {
                            String::new()
                        }
                    });
                normalize_notify(Some(&ch), Some(&tg))?
            }
        };

    let next = if cur.enabled {
        Some(next_run_at_ms(&freq, &time_of_day, weekday, now_ms))
    } else {
        None
    };

    store.with_conn(|c| {
        c.execute(
            "UPDATE scheduled_tasks SET name = ?2, description = ?3, freq = ?4, time_of_day = ?5, \
             weekday = ?6, workspace_id = ?7, next_run_at_ms = ?8, updated_at_ms = ?9, \
             notify_channel = ?10, notify_target = ?11 \
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
                notify_channel,
                notify_target,
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
    row.notify_channel = notify_channel;
    row.notify_target = notify_target;
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
            notify_channel: None,
            notify_target: None,
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

    #[test]
    fn test_parse_natural_daily_with_period_and_name() {
        let p = parse_natural("每天上午9点汇总品牌舆情").unwrap();
        assert_eq!(p.freq, FREQ_DAILY);
        assert_eq!(p.time_of_day, "09:00");
        assert_eq!(p.weekday, None);
        assert_eq!(p.name, "汇总品牌舆情");
        assert_eq!(p.note, None);

        let p = parse_natural("每天下午 3 点半提醒我提交日报").unwrap();
        assert_eq!(p.time_of_day, "15:30");
        assert_eq!(p.name, "提交日报");
    }

    #[test]
    fn test_parse_natural_weekly_and_monthly() {
        let p = parse_natural("每周一上午 9:00 生成周报").unwrap();
        assert_eq!(p.freq, FREQ_WEEKLY);
        assert_eq!(p.weekday, Some(1));
        assert_eq!(p.time_of_day, "09:00");
        assert_eq!(p.name, "生成周报");

        let p = parse_natural("每周日晚上8点备份数据").unwrap();
        assert_eq!(p.weekday, Some(7));
        assert_eq!(p.time_of_day, "20:00");
        assert_eq!(p.name, "备份数据");

        let p = parse_natural("每月1号凌晨2点清理缓存").unwrap();
        assert_eq!(p.freq, FREQ_MONTHLY);
        assert_eq!(p.time_of_day, "02:00");
        assert_eq!(p.name, "清理缓存");
    }

    #[test]
    fn test_parse_natural_hourly() {
        let p = parse_natural("每小时的第30分钟检查服务").unwrap();
        assert_eq!(p.freq, FREQ_HOURLY);
        assert_eq!(p.time_of_day, "00:30");
        assert_eq!(p.name, "检查服务");

        let p = parse_natural("每小时 15 分同步数据").unwrap();
        assert_eq!(p.time_of_day, "00:15");
        assert_eq!(p.name, "同步数据");
    }

    #[test]
    fn test_parse_natural_defaults_to_daily_with_note() {
        let p = parse_natural("上午10点开晨会").unwrap();
        assert_eq!(p.freq, FREQ_DAILY);
        assert_eq!(p.time_of_day, "10:00");
        assert_eq!(p.name, "开晨会");
        assert_eq!(p.note.as_deref(), Some("未写频率，已按每天处理"));
    }

    #[test]
    fn test_parse_natural_night_and_noon_edges() {
        assert_eq!(parse_natural("每天晚上12点归档").unwrap().time_of_day, "00:00");
        assert_eq!(parse_natural("每天中午1点午会").unwrap().time_of_day, "13:00");
        assert_eq!(parse_natural("每天中午12点午餐").unwrap().time_of_day, "12:00");
        assert_eq!(parse_natural("每天24:00 不允许").unwrap_err().len() > 0, true);
    }

    #[test]
    fn test_parse_natural_rejects_unsupported_and_missing_time() {
        let err = parse_natural("每2小时同步一次").unwrap_err();
        assert!(err.starts_with("SCHEDULE_UNSUPPORTED_FREQ:"), "{err}");
        let err = parse_natural("每隔30分钟检查").unwrap_err();
        assert!(err.starts_with("SCHEDULE_UNSUPPORTED_FREQ:"), "{err}");
        let err = parse_natural("每月15号发工资").unwrap_err();
        assert!(err.starts_with("SCHEDULE_UNSUPPORTED_DAY:"), "{err}");
        let err = parse_natural("每天汇总舆情").unwrap_err();
        assert!(err.starts_with("SCHEDULE_PARSE_NO_TIME:"), "{err}");
        let err = parse_natural("  ").unwrap_err();
        assert!(err.starts_with("SCHEDULE_PARSE_EMPTY:"), "{err}");
    }

    #[test]
    fn test_normalize_notify_pairs_channel_and_target() {
        assert_eq!(normalize_notify(None, None).unwrap(), (String::new(), String::new()));
        assert_eq!(
            normalize_notify(Some(" Slack "), Some(" C123 ")).unwrap(),
            ("slack".into(), "C123".into())
        );
        assert!(normalize_notify(Some(""), Some("C123"))
            .unwrap_err()
            .starts_with("SCHEDULE_INVALID_NOTIFY:"));
        assert!(normalize_notify(Some("slack"), Some(""))
            .unwrap_err()
            .starts_with("SCHEDULE_INVALID_NOTIFY:"));
        assert!(normalize_notify(Some("sl/ack"), Some("C1"))
            .unwrap_err()
            .starts_with("SCHEDULE_INVALID_NOTIFY:"));
    }

    #[test]
    fn test_create_and_update_notify_fields() {
        let (store, dir) = temp_store("notify");
        let mut req = new_req("每天", "08:00", None);
        req.notify_channel = Some("slack".into());
        req.notify_target = Some("C123".into());
        let row = create(&store, &req, base_ms()).unwrap();
        assert_eq!(row.notify_channel, "slack");
        assert_eq!(row.notify_target, "C123");
        assert_eq!(get(&store, &row.schedule_id).unwrap().unwrap(), row);

        // 只改目标：沿用通道
        let edited = update(
            &store,
            &UpdateSchedule {
                schedule_id: row.schedule_id.clone(),
                notify_target: Some("C999".into()),
                ..Default::default()
            },
            base_ms(),
        )
        .unwrap();
        assert_eq!(edited.notify_channel, "slack");
        assert_eq!(edited.notify_target, "C999");

        // 切换通道但没给目标：不继承旧会话，直接拒绝
        let err = update(
            &store,
            &UpdateSchedule {
                schedule_id: row.schedule_id.clone(),
                notify_channel: Some("dingtalk".into()),
                ..Default::default()
            },
            base_ms(),
        )
        .unwrap_err();
        assert!(err.starts_with("SCHEDULE_INVALID_NOTIFY:"), "{err}");

        // 通道置空：连目标一起关闭
        let cleared = update(
            &store,
            &UpdateSchedule {
                schedule_id: row.schedule_id.clone(),
                notify_channel: Some("".into()),
                ..Default::default()
            },
            base_ms(),
        )
        .unwrap();
        assert_eq!(cleared.notify_channel, "");
        assert_eq!(cleared.notify_target, "");
        let _ = std::fs::remove_dir_all(&dir);
    }

    impl NewSchedule {
        fn with_name(mut self, name: &str) -> Self {
            self.name = name.into();
            self
        }
    }
}