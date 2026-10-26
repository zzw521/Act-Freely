//! 模型用量台账（真实 token 记账）
//!
//! 数据来源：引擎执行流上报的 `usage` 事件（`bridge/agent.rs::persist_and_meter`），
//! 按「模型 × UTC 日」增量累加进 `model_usage` 表（V3 迁移，纯新增，不触碰存量数据）。
//!
//! 周期语义（与前端 ModelStatsPanel 的 daily/monthly/total 对齐）：
//! - daily：今日（UTC 日与引擎台账同口径）
//! - monthly：本月
//! - total：全部历史
//!
//! 展示名：优先取 `model_configs` 中同 `model_name` 的配置名，未配置则用模型名本身。
//!
//! 三种读法共用同一台账与同一周期过滤（`period_filter`），口径不会互相打架：
//! - `query`：每模型明细（`model:usage`）
//! - `stats`：周期汇总（`usage:stats`，顶部总览条）
//! - `report`：按日趋势 + 明细 + 汇总（`usage:report`，可复制成报表文本）
//!
//! 台账存 token、调用次数、端到端延迟与按币种成本；旧数据没有延迟/单价时
//! 如实返回 `avgLatencyMs = null` 与空成本清单，不拿 0 冒充真实测量值。

use serde::{Deserialize, Serialize};

use crate::store::Store;

/// 按币种分桶的成本（micros，1 货币单位 = 1,000,000 micros）
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UsageCost {
    pub currency: String,
    pub micros: i64,
}

/// 用量行（前端 ModelUsageStats 的数据源，camelCase 对齐前端）
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UsageRow {
    /// 引擎上报的模型名（作为统计主键）
    pub model_name: String,
    /// 展示名（model_configs 配置名，缺省等于 modelName）
    pub display_name: String,
    pub prompt_tokens: i64,
    pub completion_tokens: i64,
    /// prompt + completion
    pub tokens_used: i64,
    pub calls: i64,
    pub last_used_at_ms: i64,
    /// 平均端到端耗时；没有延迟样本（旧数据）时为 null
    pub avg_latency_ms: Option<i64>,
    /// 参与平均的调用数
    pub latency_samples: i64,
    /// 按币种分开的成本；未配置单价的调用不计入
    pub costs: Vec<UsageCost>,
    /// 计价 / 未计价调用次数（未配置单价不能当 0 元）
    pub priced_calls: i64,
    pub unpriced_calls: i64,
}

/// UTC 日期 `yyyy-mm-dd`（复用审计模块的自实现历法，避免引入 chrono）
fn utc_day(ms: i64) -> String {
    crate::security::audit::iso8601_utc(ms)[..10].to_string()
}

fn utc_month(ms: i64) -> String {
    crate::security::audit::iso8601_utc(ms)[..7].to_string()
}

/// 周期 → 台账过滤条件（`query`/`daily_points` 共用，保证总览、明细、趋势口径一致）
///
/// 时间窗作为参数传入（不拼接字面量，避免注入）。
fn period_filter(period: &str, now: i64) -> (&'static str, Option<String>) {
    match period {
        "daily" => ("WHERE u.day = ?1", Some(utc_day(now))),
        "monthly" => ("WHERE u.day LIKE ?1", Some(format!("{}%", utc_month(now)))),
        _ => ("", None),
    }
}

/// 周期合法性：非法值返回错误码（不静默降级，便于前端暴露问题）
pub fn normalize_period(period: &str) -> Result<String, String> {
    match period {
        "daily" | "monthly" | "total" => Ok(period.to_string()),
        other => Err(format!("USAGE_INVALID_PERIOD: {other}")),
    }
}

/// 记一次模型调用（增量累加当日行；model_name 为空时忽略）
pub fn record(
    store: &Store,
    model_name: &str,
    prompt_tokens: u64,
    completion_tokens: u64,
) -> Result<(), String> {
    record_sample(store, model_name, prompt_tokens, completion_tokens, None, None, None, false)
}

/// 记一次带延迟与成本的模型调用。
///
/// - 延迟为端到端耗时（流式为整段生成耗时），缺失时只累加 token/次数；
/// - 成本只有配置了单价才有值，按调用时的币种写入 `model_usage_cost`；
/// - 未计价调用与已计价调用分开统计，不能把「没配单价」当 0 元。
#[allow(clippy::too_many_arguments)]
pub fn record_sample(
    store: &Store,
    model_name: &str,
    prompt_tokens: u64,
    completion_tokens: u64,
    latency_ms: Option<i64>,
    cost_micros: Option<i64>,
    currency: Option<&str>,
    priced: bool,
) -> Result<(), String> {
    let name = model_name.trim();
    if name.is_empty() {
        return Ok(());
    }
    let now = crate::bridge::event_store::now_ms() as i64;
    let day = utc_day(now);
    let latency = latency_ms.unwrap_or(0).max(0);
    let latency_samples = i64::from(latency_ms.is_some());
    let cost = cost_micros.filter(|v| *v >= 0);
    let currency = currency
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or("CNY")
        .to_uppercase();
    store.with_conn(|c| {
        c.execute(
            "INSERT INTO model_usage (model_name, day, prompt_tokens, completion_tokens, calls, \
                 last_used_at_ms, latency_ms_total, latency_samples) \
             VALUES (?1, ?2, ?3, ?4, 1, ?5, ?6, ?7) \
             ON CONFLICT(model_name, day) DO UPDATE SET \
                 prompt_tokens = prompt_tokens + excluded.prompt_tokens, \
                 completion_tokens = completion_tokens + excluded.completion_tokens, \
                 calls = calls + excluded.calls, \
                 last_used_at_ms = excluded.last_used_at_ms, \
                 latency_ms_total = latency_ms_total + excluded.latency_ms_total, \
                 latency_samples = latency_samples + excluded.latency_samples",
            rusqlite::params![
                name,
                day,
                prompt_tokens as i64,
                completion_tokens as i64,
                now,
                latency,
                latency_samples
            ],
        )
        .map_err(|e| format!("USAGE_RECORD_FAILED: {e}"))?;
        if priced {
            if let Some(cost) = cost {
                c.execute(
                    "INSERT INTO model_usage_cost (model_name, day, currency, cost_micros, priced_calls) \
                     VALUES (?1, ?2, ?3, ?4, 1) \
                     ON CONFLICT(model_name, day, currency) DO UPDATE SET \
                         cost_micros = cost_micros + excluded.cost_micros, \
                         priced_calls = priced_calls + excluded.priced_calls",
                    rusqlite::params![name, day, currency, cost],
                )
                .map_err(|e| format!("USAGE_RECORD_FAILED: {e}"))?;
            }
        }
        Ok(())
    })
}

/// 尽力而为的记账：写入失败不阻断任务执行
pub fn record_quiet(store: &Store, model_name: &str, prompt_tokens: u64, completion_tokens: u64) {
    if let Err(e) = record(store, model_name, prompt_tokens, completion_tokens) {
        eprintln!("[usage] 记账失败（不阻断执行）: {e}");
    }
}

/// 带延迟/成本的尽力而为记账
#[allow(clippy::too_many_arguments)]
pub fn record_sample_quiet(
    store: &Store,
    model_name: &str,
    prompt_tokens: u64,
    completion_tokens: u64,
    latency_ms: Option<i64>,
    cost_micros: Option<i64>,
    currency: Option<&str>,
    priced: bool,
) {
    if let Err(e) = record_sample(
        store,
        model_name,
        prompt_tokens,
        completion_tokens,
        latency_ms,
        cost_micros,
        currency,
        priced,
    ) {
        eprintln!("[usage] 记账失败（不阻断执行）: {e}");
    }
}

/// 成本按模型/日期查询的周期过滤（列名与主表不同，单独构造避免拼接用户输入）
fn cost_period_filter(period: &str, now: i64) -> (&'static str, Option<String>) {
    match period {
        "daily" => ("WHERE c.day = ?1", Some(utc_day(now))),
        "monthly" => ("WHERE c.day LIKE ?1", Some(format!("{}%", utc_month(now)))),
        _ => ("", None),
    }
}

/// 成本明细：model_name → (按币种成本, 计价调用数)
fn costs_by_model(
    store: &Store,
    period: &str,
    now: i64,
) -> Result<std::collections::HashMap<String, (Vec<UsageCost>, i64)>, String> {
    let (where_sql, arg) = cost_period_filter(period, now);
    let sql = format!(
        "SELECT c.model_name, c.currency, SUM(c.cost_micros), SUM(c.priced_calls) \
         FROM model_usage_cost c {where_sql} \
         GROUP BY c.model_name, c.currency \
         ORDER BY c.model_name ASC, c.currency ASC"
    );
    store.with_conn(|c| {
        let mut stmt = c
            .prepare(&sql)
            .map_err(|e| format!("USAGE_QUERY_FAILED: {e}"))?;
        let map = |r: &rusqlite::Row<'_>| -> rusqlite::Result<(String, UsageCost, i64)> {
            Ok((
                r.get(0)?,
                UsageCost { currency: r.get(1)?, micros: r.get(2)? },
                r.get(3)?,
            ))
        };
        let mut out: std::collections::HashMap<String, (Vec<UsageCost>, i64)> =
            std::collections::HashMap::new();
        let rows = match arg {
            Some(a) => stmt.query_map([a], map),
            None => stmt.query_map([], map),
        }
        .map_err(|e| format!("USAGE_QUERY_FAILED: {e}"))?;
        for row in rows {
            let (model, cost, priced) =
                row.map_err(|e| format!("USAGE_QUERY_FAILED: {e}"))?;
            let entry = out.entry(model).or_default();
            entry.0.push(cost);
            entry.1 += priced;
        }
        Ok(out)
    })
}

/// 按周期聚合查询（period 已由 `normalize_period` 校验）
pub fn query(store: &Store, period: &str) -> Result<Vec<UsageRow>, String> {
    let period = normalize_period(period)?;
    let now = crate::bridge::event_store::now_ms() as i64;
    let (where_sql, arg) = period_filter(&period, now);
    let costs = costs_by_model(store, &period, now)?;
    let sql = format!(
        "SELECT u.model_name, \
                COALESCE((SELECT m.name FROM model_configs m WHERE m.model_name = u.model_name \
                          ORDER BY m.is_default DESC, m.updated_at DESC LIMIT 1), u.model_name) AS display_name, \
                SUM(u.prompt_tokens), SUM(u.completion_tokens), SUM(u.calls), MAX(u.last_used_at_ms), \
                SUM(u.latency_ms_total), SUM(u.latency_samples) \
         FROM model_usage u {where_sql} \
         GROUP BY u.model_name \
         ORDER BY SUM(u.prompt_tokens) + SUM(u.completion_tokens) DESC, u.model_name ASC"
    );

    store.with_conn(|c| {
        let mut stmt = c.prepare(&sql).map_err(|e| format!("USAGE_QUERY_FAILED: {e}"))?;
        let map_row = |r: &rusqlite::Row<'_>| -> rusqlite::Result<UsageRow> {
            let prompt: i64 = r.get(2)?;
            let completion: i64 = r.get(3)?;
            let calls: i64 = r.get(4)?;
            let latency_total: i64 = r.get(6)?;
            let latency_samples: i64 = r.get(7)?;
            let model_name: String = r.get(0)?;
            let (model_costs, priced_calls) = costs
                .get(&model_name)
                .cloned()
                .unwrap_or_else(|| (Vec::new(), 0));
            Ok(UsageRow {
                model_name,
                display_name: r.get(1)?,
                prompt_tokens: prompt,
                completion_tokens: completion,
                tokens_used: prompt + completion,
                calls,
                last_used_at_ms: r.get(5)?,
                avg_latency_ms: if latency_samples > 0 {
                    Some(latency_total / latency_samples)
                } else {
                    None
                },
                latency_samples,
                costs: model_costs,
                priced_calls,
                unpriced_calls: (calls - priced_calls).max(0),
            })
        };
        let rows = match arg {
            Some(a) => stmt.query_map([a], map_row).map_err(|e| format!("USAGE_QUERY_FAILED: {e}"))?,
            None => stmt.query_map([], map_row).map_err(|e| format!("USAGE_QUERY_FAILED: {e}"))?,
        };
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("USAGE_QUERY_FAILED: {e}"))
    })
}

/// 周期汇总（`usage:stats` 与 `usage:report` 共用）
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UsageTotals {
    pub prompt_tokens: i64,
    pub completion_tokens: i64,
    pub tokens_used: i64,
    pub calls: i64,
    /// 周期内产生过用量的模型数
    pub active_models: usize,
    /// 周期内最后一次调用时间（无用量为 0）
    pub last_used_at_ms: i64,
    /// 按调用数加权的平均耗时；没有延迟样本时为 null
    pub avg_latency_ms: Option<i64>,
    pub latency_samples: i64,
    pub costs: Vec<UsageCost>,
    pub priced_calls: i64,
    pub unpriced_calls: i64,
}

fn merge_cost(target: &mut Vec<UsageCost>, source: &[UsageCost]) {
    for cost in source {
        match target.iter_mut().find(|c| c.currency == cost.currency) {
            Some(slot) => slot.micros += cost.micros,
            None => target.push(cost.clone()),
        }
    }
    target.sort_by(|a, b| a.currency.cmp(&b.currency));
}

fn totals_of(rows: &[UsageRow]) -> UsageTotals {
    let mut t = UsageTotals {
        prompt_tokens: 0,
        completion_tokens: 0,
        tokens_used: 0,
        calls: 0,
        active_models: rows.len(),
        last_used_at_ms: 0,
        avg_latency_ms: None,
        latency_samples: 0,
        costs: Vec::new(),
        priced_calls: 0,
        unpriced_calls: 0,
    };
    let mut weighted_latency: i64 = 0;
    for r in rows {
        t.prompt_tokens += r.prompt_tokens;
        t.completion_tokens += r.completion_tokens;
        t.tokens_used += r.tokens_used;
        t.calls += r.calls;
        t.last_used_at_ms = t.last_used_at_ms.max(r.last_used_at_ms);
        t.latency_samples += r.latency_samples;
        weighted_latency += r.avg_latency_ms.unwrap_or(0) * r.latency_samples;
        merge_cost(&mut t.costs, &r.costs);
        t.priced_calls += r.priced_calls;
        t.unpriced_calls += r.unpriced_calls;
    }
    t.avg_latency_ms = if t.latency_samples > 0 {
        Some(weighted_latency / t.latency_samples)
    } else {
        None
    };
    t
}

/// 总览返回体（前端 `UsageStats`：顶部总览条的数据源）
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct StatsView {
    pub period: String,
    pub totals: UsageTotals,
    /// 用量最高模型的展示名（无用量为 null）
    pub top_model: Option<String>,
}

/// 按日趋势点（前端 `UsageDayPoint`；day 为 UTC 日 `yyyy-mm-dd`）
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DayPoint {
    pub day: String,
    pub tokens_used: i64,
    pub calls: i64,
    /// 当日活跃模型数
    pub models: usize,
    /// 当日平均耗时；没有延迟样本时为 null
    pub avg_latency_ms: Option<i64>,
    pub latency_samples: i64,
    pub costs: Vec<UsageCost>,
    pub priced_calls: i64,
    pub unpriced_calls: i64,
}

/// 报表返回体（前端 `UsageReport`：趋势 + 明细 + 汇总，自包含便于导出）
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ReportView {
    pub period: String,
    pub generated_at_ms: i64,
    pub days: Vec<DayPoint>,
    pub models: Vec<UsageRow>,
    pub totals: UsageTotals,
}

/// 周期汇总查询（非法周期沿用 `USAGE_INVALID_PERIOD`，不静默按 total 兜底）
pub fn stats(store: &Store, period: &str) -> Result<StatsView, String> {
    let period = normalize_period(period)?;
    let rows = query(store, &period)?;
    let top_model = rows.first().map(|r| r.display_name.clone());
    Ok(StatsView {
        totals: totals_of(&rows),
        period,
        top_model,
    })
}

/// 成本按日分桶（趋势图用）
fn costs_by_day(
    store: &Store,
    period: &str,
    now: i64,
) -> Result<std::collections::HashMap<String, (Vec<UsageCost>, i64)>, String> {
    let (where_sql, arg) = cost_period_filter(period, now);
    let sql = format!(
        "SELECT c.day, c.currency, SUM(c.cost_micros), SUM(c.priced_calls) \
         FROM model_usage_cost c {where_sql} \
         GROUP BY c.day, c.currency \
         ORDER BY c.day ASC, c.currency ASC"
    );
    store.with_conn(|c| {
        let mut stmt = c
            .prepare(&sql)
            .map_err(|e| format!("USAGE_REPORT_FAILED: {e}"))?;
        let map = |r: &rusqlite::Row<'_>| -> rusqlite::Result<(String, UsageCost, i64)> {
            Ok((
                r.get(0)?,
                UsageCost { currency: r.get(1)?, micros: r.get(2)? },
                r.get(3)?,
            ))
        };
        let mut out: std::collections::HashMap<String, (Vec<UsageCost>, i64)> =
            std::collections::HashMap::new();
        let rows = match arg {
            Some(a) => stmt.query_map([a], map),
            None => stmt.query_map([], map),
        }
        .map_err(|e| format!("USAGE_REPORT_FAILED: {e}"))?;
        for row in rows {
            let (day, cost, priced) =
                row.map_err(|e| format!("USAGE_REPORT_FAILED: {e}"))?;
            let entry = out.entry(day).or_default();
            entry.0.push(cost);
            entry.1 += priced;
        }
        Ok(out)
    })
}

fn daily_points(store: &Store, period: &str, now: i64) -> Result<Vec<DayPoint>, String> {
    let (where_sql, arg) = period_filter(period, now);
    let costs = costs_by_day(store, period, now)?;
    let sql = format!(
        "SELECT u.day, SUM(u.prompt_tokens) + SUM(u.completion_tokens), SUM(u.calls), \
                COUNT(DISTINCT u.model_name), SUM(u.latency_ms_total), SUM(u.latency_samples) \
         FROM model_usage u {where_sql} \
         GROUP BY u.day \
         ORDER BY u.day ASC"
    );
    store.with_conn(|c| {
        let mut stmt = c
            .prepare(&sql)
            .map_err(|e| format!("USAGE_REPORT_FAILED: {e}"))?;
        let map = |r: &rusqlite::Row<'_>| -> rusqlite::Result<DayPoint> {
            let models: i64 = r.get(3)?;
            let latency_total: i64 = r.get(4)?;
            let latency_samples: i64 = r.get(5)?;
            let day: String = r.get(0)?;
            let calls: i64 = r.get(2)?;
            let (day_costs, priced_calls) = costs
                .get(&day)
                .cloned()
                .unwrap_or_else(|| (Vec::new(), 0));
            Ok(DayPoint {
                avg_latency_ms: if latency_samples > 0 {
                    Some(latency_total / latency_samples)
                } else {
                    None
                },
                latency_samples,
                costs: day_costs,
                priced_calls,
                unpriced_calls: (calls - priced_calls).max(0),
                day,
                tokens_used: r.get(1)?,
                calls,
                models: models.max(0) as usize,
            })
        };
        let rows = match arg {
            Some(a) => stmt
                .query_map([a], map)
                .map_err(|e| format!("USAGE_REPORT_FAILED: {e}"))?,
            None => stmt
                .query_map([], map)
                .map_err(|e| format!("USAGE_REPORT_FAILED: {e}"))?,
        };
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("USAGE_REPORT_FAILED: {e}"))
    })
}

/// 用量报表查询（趋势按 UTC 日分桶，与明细同一周期过滤口径）
pub fn report(store: &Store, period: &str) -> Result<ReportView, String> {
    let period = normalize_period(period)?;
    let now = crate::bridge::event_store::now_ms() as i64;
    let days = daily_points(store, &period, now)?;
    let models = query(store, &period)?;
    let totals = totals_of(&models);
    Ok(ReportView {
        period,
        generated_at_ms: now,
        days,
        models,
        totals,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn temp_store(name: &str) -> (Store, PathBuf) {
        let dir = std::env::temp_dir().join(format!(
            "tt-usage-{name}-{}",
            crate::bridge::event_store::now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let store = Store::open(dir.join("t.db")).unwrap();
        (store, dir)
    }

    #[test]
    fn test_record_accumulates_per_model_and_day() {
        let (store, dir) = temp_store("acc");
        record(&store, "qwen-plus", 100, 50).unwrap();
        record(&store, "qwen-plus", 20, 10).unwrap();
        record(&store, "qwen-max", 7, 3).unwrap();

        let rows = query(&store, "daily").unwrap();
        assert_eq!(rows.len(), 2);
        // 按总 token 倒序：qwen-plus(180) 在前
        assert_eq!(rows[0].model_name, "qwen-plus");
        assert_eq!(rows[0].prompt_tokens, 120);
        assert_eq!(rows[0].completion_tokens, 60);
        assert_eq!(rows[0].tokens_used, 180);
        assert_eq!(rows[0].calls, 2);
        assert!(rows[0].last_used_at_ms > 0);
        assert_eq!(rows[1].model_name, "qwen-max");
        assert_eq!(rows[1].tokens_used, 10);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_display_name_falls_back_to_model_name() {
        let (store, dir) = temp_store("display");
        record(&store, "qwen-plus", 1, 1).unwrap();
        let rows = query(&store, "total").unwrap();
        assert_eq!(rows[0].display_name, "qwen-plus", "未配置模型时展示名回落为模型名");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_record_ignores_empty_model_name() {
        let (store, dir) = temp_store("empty-name");
        record(&store, "   ", 100, 100).unwrap();
        assert!(query(&store, "total").unwrap().is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_query_empty_table() {
        let (store, dir) = temp_store("empty");
        assert!(query(&store, "monthly").unwrap().is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_invalid_period_rejected() {
        let (store, dir) = temp_store("period");
        assert!(query(&store, "hourly").unwrap_err().starts_with("USAGE_INVALID_PERIOD"));
        assert_eq!(normalize_period("daily").unwrap(), "daily");
        assert!(normalize_period("").is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// 周期过滤：昨日/上月分桶不被 daily/monthly 命中，但计入 total
    #[test]
    fn test_period_filters_bucket_by_day() {
        let (store, dir) = temp_store("period-filter");
        let now = crate::bridge::event_store::now_ms() as i64;
        let today = utc_day(now);
        let yesterday = utc_day(now - 86_400_000);
        let last_month = utc_day(now - 40 * 86_400_000);
        store
            .with_conn(|c| {
                for (day, tokens) in [(&today, 10i64), (&yesterday, 20), (&last_month, 30)] {
                    c.execute(
                        "INSERT INTO model_usage (model_name, day, prompt_tokens, completion_tokens, calls, last_used_at_ms) \
                         VALUES ('qwen-plus', ?1, ?2, 0, 1, 1)",
                        rusqlite::params![day, tokens],
                    )
                    .map_err(|e| e.to_string())?;
                }
                Ok(())
            })
            .unwrap();

        assert_eq!(query(&store, "daily").unwrap()[0].tokens_used, 10);
        let monthly = query(&store, "monthly").unwrap();
        // 本月 = 今日 + 昨日（跨月边界日仍可能同月，故断言「不含上月」而非精确值）
        assert!(monthly[0].tokens_used >= 10 && monthly[0].tokens_used <= 30);
        let total = query(&store, "total").unwrap()[0].tokens_used;
        assert_eq!(total, 60);
        assert_eq!(query(&store, "total").unwrap()[0].calls, 3);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_record_quiet_swallows_failure() {
        let (store, dir) = temp_store("quiet");
        store
            .with_conn(|c| c.execute_batch("DROP TABLE model_usage").map_err(|e| e.to_string()))
            .unwrap();
        record_quiet(&store, "qwen-plus", 1, 1);
        assert!(query(&store, "total").is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// 直接写入指定日期分桶（构造跨日/跨月趋势，绕开「现在」的单日限制）
    fn put_day(store: &Store, model: &str, day: &str, prompt: i64, completion: i64) {
        store
            .with_conn(|c| {
                c.execute(
                    "INSERT INTO model_usage (model_name, day, prompt_tokens, completion_tokens, calls, last_used_at_ms) \
                     VALUES (?1, ?2, ?3, ?4, 1, 1)",
                    rusqlite::params![model, day, prompt, completion],
                )
                .map(|_| ())
                .map_err(|e| e.to_string())
            })
            .unwrap();
    }

    #[test]
    fn test_stats_totals_and_top_model() {
        let (store, dir) = temp_store("stats");
        record(&store, "qwen-plus", 100, 50).unwrap();
        record(&store, "qwen-plus", 20, 10).unwrap();
        record(&store, "qwen-max", 7, 3).unwrap();

        let view = stats(&store, "daily").unwrap();
        assert_eq!(view.period, "daily");
        assert_eq!(view.totals.prompt_tokens, 127);
        assert_eq!(view.totals.completion_tokens, 63);
        assert_eq!(view.totals.tokens_used, 190);
        assert_eq!(view.totals.calls, 3);
        assert_eq!(view.totals.active_models, 2);
        assert!(view.totals.last_used_at_ms > 0);
        // 明细按总 token 倒序，故用量最高者即 topModel
        assert_eq!(view.top_model.as_deref(), Some("qwen-plus"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_stats_on_empty_ledger_has_no_top_model() {
        let (store, dir) = temp_store("stats-empty");
        let view = stats(&store, "total").unwrap();
        assert_eq!(view.totals.tokens_used, 0);
        assert_eq!(view.totals.calls, 0);
        assert_eq!(view.totals.active_models, 0);
        assert_eq!(view.totals.last_used_at_ms, 0);
        assert_eq!(view.top_model, None, "无用量时不伪造 top 模型");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_stats_and_report_reject_invalid_period() {
        let (store, dir) = temp_store("stats-period");
        assert!(stats(&store, "hourly")
            .unwrap_err()
            .starts_with("USAGE_INVALID_PERIOD"));
        assert!(report(&store, "")
            .unwrap_err()
            .starts_with("USAGE_INVALID_PERIOD"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_report_day_buckets_follow_period_window() {
        let (store, dir) = temp_store("report-days");
        let now = crate::bridge::event_store::now_ms() as i64;
        let today = utc_day(now);
        let two_days_ago = utc_day(now - 2 * 86_400_000);
        let far_day = utc_day(now - 40 * 86_400_000);
        put_day(&store, "qwen-plus", &today, 60, 40);
        put_day(&store, "qwen-max", &today, 10, 10);
        put_day(&store, "qwen-plus", &two_days_ago, 25, 25);
        put_day(&store, "qwen-plus", &far_day, 999, 1);

        // total：全部日期分桶，按日升序
        let all = report(&store, "total").unwrap();
        assert_eq!(all.days.len(), 3);
        assert_eq!(all.days[0].day, far_day);
        assert_eq!(all.days[2].day, today);
        assert_eq!(all.days[2].tokens_used, 120);
        assert_eq!(all.days[2].models, 2, "当日活跃模型数按 DISTINCT 统计");
        assert_eq!(all.days[2].calls, 2);

        // daily：只含今日
        let daily = report(&store, "daily").unwrap();
        assert_eq!(daily.days.len(), 1);
        assert_eq!(daily.days[0].day, today);
        assert_eq!(daily.totals.tokens_used, 120);

        // monthly：今日所在月，远月分桶不得混入
        let monthly = report(&store, "monthly").unwrap();
        let month = utc_month(now);
        assert!(monthly.days.iter().all(|p| p.day.starts_with(&month)));
        assert!(monthly.days.iter().any(|p| p.day == today));
        assert!(monthly.totals.tokens_used < 1120);
        assert!(!monthly.days.iter().any(|p| p.day == far_day));
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// 报表内部自洽：趋势合计 == 明细合计 == totals（同一周期过滤，口径不得打架）
    #[test]
    fn test_report_internally_consistent() {
        let (store, dir) = temp_store("report-consistent");
        let now = crate::bridge::event_store::now_ms() as i64;
        put_day(&store, "qwen-plus", &utc_day(now), 60, 40);
        put_day(&store, "qwen-plus", &utc_day(now - 86_400_000), 5, 5);
        put_day(&store, "qwen-max", &utc_day(now - 86_400_000), 2, 1);

        let rep = report(&store, "total").unwrap();
        let day_sum: i64 = rep.days.iter().map(|p| p.tokens_used).sum();
        let model_sum: i64 = rep.models.iter().map(|m| m.tokens_used).sum();
        assert_eq!(day_sum, rep.totals.tokens_used);
        assert_eq!(model_sum, rep.totals.tokens_used);
        assert_eq!(rep.totals.calls, 3);
        assert_eq!(rep.totals.active_models, rep.models.len());
        assert!(rep.generated_at_ms >= now);
        assert_eq!(rep.period, "total");
        let _ = std::fs::remove_dir_all(&dir);
    }
}